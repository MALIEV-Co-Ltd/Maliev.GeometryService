"""Overlay GLB generation and upload helpers for DFM visualization.

This module provides functions to generate and upload overlay GLBs
that highlight specific manufacturing issues (thin walls, overhangs, etc.).
"""

import asyncio
import base64
import contextlib
import logging
from typing import Any
from uuid import uuid4

import httpx

from src.core.geometry import _generate_overlays_from_paths

logger = logging.getLogger(__name__)


async def generate_and_upload_overlays(
    glb_path: str,
    reports: dict[str, Any],
    storage_path: str,
    upload_service_url: str,
    token_provider: Any,
    http_client: httpx.AsyncClient,
    upload_id: str,
    stl_bytes: bytes | None = None,
    cad_glb_bytes: bytes | None = None,
    cad_extension: str | None = None,
) -> dict[str, str]:
    """Generate overlay GLBs and upload them to GCS.

    Args:
        glb_path: Path to the source GLB file (used only as a fallback/cache key).
        reports: DFM analysis reports dict (keyed by process code).
        storage_path: Base GCS storage path for the file (without extension).
        upload_service_url: Upload Service base URL for artifact uploads.
        token_provider: Token provider for authenticating with Upload Service.
        http_client: Shared httpx.AsyncClient for making HTTP requests.
        upload_id: Upload identifier for tracking.
        stl_bytes: Pre-loaded STL bytes. When provided, bypasses GLB file loading
                   entirely — the mesh is created directly from these bytes.
        cad_glb_bytes: Source CAD GLB bytes used to compute the displayed viewer
                       GLB center.
        cad_extension: Original CAD extension used for the viewer GLB unit rules.

    Returns:
        Dictionary mapping overlay keys (e.g. "FDM__thin_wall") to GCS paths.
    """
    import asyncio

    loop = asyncio.get_event_loop()

    # Import dfm_executor from geometry module for the executor
    from src.core.geometry import (
        GeometryProcessor,
        _compute_viewer_glb_reference_center,
        _generate_overlays_worker,
    )

    # Use a temporary executor for this one-off operation
    processor = GeometryProcessor()
    dfm_executor = processor.dfm_executor

    overlay_paths: dict[str, str] = {}
    try:
        reference_center = _compute_viewer_glb_reference_center(
            cad_glb_bytes,
            cad_extension,
        )
        if stl_bytes is not None:
            # Fast path: use already-available STL bytes directly,
            # skipping the GLB file load entirely.
            import io as _io

            import trimesh

            try:
                mesh = trimesh.load(
                    _io.BytesIO(stl_bytes), file_type="stl", force="mesh"
                )
                if isinstance(mesh, trimesh.Trimesh) and len(mesh.vertices) > 0:
                    mesh_bytes_dict: dict[int, bytes] = {0: stl_bytes}
                    overlay_glbs: dict[str, bytes] = await loop.run_in_executor(
                        dfm_executor,
                        _generate_overlays_worker,
                        mesh_bytes_dict,
                        reports,
                        reference_center,
                    )
                else:
                    logger.warning(
                        "STL bytes produced empty mesh for %s, falling back to GLB",
                        upload_id,
                    )
                    overlay_glbs = {}
            except Exception as e:
                logger.warning(
                    "Failed to load STL bytes for overlay generation (%s), "
                    "falling back to GLB path: %s",
                    upload_id,
                    e,
                )
                overlay_glbs = {}

            if not overlay_glbs:
                # Fallback to GLB path if STL bytes didn't work
                overlay_glbs = await loop.run_in_executor(
                    dfm_executor,
                    _generate_overlays_from_paths,
                    glb_path,
                    reports,
                )
        else:
            # Generate overlay GLBs from GLB file (returns dict[str, bytes])
            overlay_glbs = await loop.run_in_executor(
                dfm_executor,
                _generate_overlays_from_paths,
                glb_path,
                reports,
            )

        # Upload each overlay GLB to GCS
        for overlay_key, glb_bytes in overlay_glbs.items():
            overlay_path = f"{storage_path}_{overlay_key}_overlay.glb"
            # Upload to GCS (non-blocking: continue on failure)
            success = await _upload_artifact(
                upload_service_url,
                token_provider,
                http_client,
                glb_bytes,
                overlay_path,
                "model/gltf-binary",
                upload_id,
            )
            if success:
                overlay_paths[overlay_key] = overlay_path
                logger.debug(
                    f"Uploaded overlay: {overlay_key} → {overlay_paths[overlay_key]}"
                )
            else:
                logger.warning(f"Failed to upload overlay: {overlay_key}")

        # Processes that produce no overlays by design (unsupported or skipped in worker).  # noqa: E501
        _SKIP_OVERLAY_PROCESSES: frozenset[str] = frozenset({"CNC_TURN"})  # noqa: N806
        _has_face_issues = any(
            issue.get("faceIndices")
            for k, report in reports.items()
            if k not in _SKIP_OVERLAY_PROCESSES
            for issue in report.get("issues", [])
        )
        if overlay_glbs and not overlay_paths:
            logger.warning(
                "Generated %d overlay GLB(s) but 0 uploaded successfully for %s",
                len(overlay_glbs),
                upload_id,
            )
        elif not overlay_glbs and reports and _has_face_issues:
            logger.warning(
                "Generated 0 overlays despite face-indexed issues (keys=%s) for %s",
                list(reports.keys()),
                upload_id,
            )
        else:
            logger.info(
                "Generated %d overlay(s) for DFM visualization (%d uploaded successfully)",  # noqa: E501
                len(overlay_glbs),
                len(overlay_paths),
            )

        # Clean up the temporary executor
        processor.dfm_executor.shutdown(wait=True)

    except Exception as e:
        logger.warning(
            "Failed to generate/upload overlay GLBs: %s",
            e,
            exc_info=True,
        )
        # Clean up the temporary executor even on error
        with contextlib.suppress(Exception):
            processor.dfm_executor.shutdown(wait=True)

    return overlay_paths


async def _upload_artifact(
    upload_service_url: str,
    token_provider: Any,
    http_client: httpx.AsyncClient,
    data: bytes,
    path: str,
    content_type: str,
    upload_id: str,
) -> bool:
    """Upload an artifact to GCS via the Upload Service HTTP endpoint.

    Args:
        upload_service_url: Upload Service base URL.
        token_provider: Token provider for authentication.
        http_client: Shared httpx.AsyncClient.
        data: Binary data to upload.
        path: GCS storage path.
        content_type: MIME content type.
        upload_id: Upload identifier for logging.

    Returns:
        True if upload succeeded, False otherwise.
    """
    try:
        b64_data = await asyncio.to_thread(
            lambda: base64.b64encode(data).decode("utf-8")
        )
        artifact_id = str(uuid4())
        payload = {
            "artifactId": artifact_id,
            "parentUploadId": upload_id,
            "storagePath": path,
            "contentType": content_type,
            "artifactData": b64_data,
        }
        size_mb = len(data) / (1024 * 1024)
        request_timeout = 30.0 + min(size_mb * 2, 270.0)
        token = token_provider.get_token()
        response = await http_client.post(
            f"{upload_service_url}/upload/v1/uploads/artifacts",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=request_timeout,
        )
        response.raise_for_status()
        logger.info(
            "Uploaded artifact for %s: %s (%d bytes)",
            upload_id,
            path,
            len(data),
        )
        return True
    except Exception as e:
        logger.warning(
            "Failed to upload artifact %s for %s: %s",
            path,
            upload_id,
            e,
        )
        return False
