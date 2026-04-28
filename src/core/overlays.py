"""Overlay GLB generation and upload helpers for DFM visualization.

This module provides functions to generate and upload overlay GLBs
that highlight specific manufacturing issues (thin walls, overhangs, etc.).
"""

import logging
from typing import Any

from src.core.geometry import _generate_overlays_from_paths
from src.infrastructure.storage import IStorageService

logger = logging.getLogger(__name__)


async def generate_and_upload_overlays(
    glb_path: str,
    reports: dict[str, Any],
    storage_path: str,
    storage_service: IStorageService,
    upload_id: str,
    stl_bytes: bytes | None = None,
) -> dict[str, str]:
    """Generate overlay GLBs and upload them to GCS.

    Args:
        glb_path: Path to the source GLB file (used only as a fallback/cache key).
        reports: DFM analysis reports dict (keyed by process code).
        storage_path: Base GCS storage path for the file (without extension).
        storage_service: Storage service for uploading artifacts.
        upload_id: Upload identifier for tracking.
        stl_bytes: Pre-loaded STL bytes. When provided, bypasses GLB file loading
                   entirely — the mesh is created directly from these bytes.

    Returns:
        Dictionary mapping overlay keys (e.g. "FDM__thin_wall") to GCS paths.
    """
    import asyncio

    loop = asyncio.get_event_loop()

    # Import dfm_executor from geometry module for the executor
    from src.core.geometry import GeometryProcessor, _generate_overlays_worker

    # Use a temporary executor for this one-off operation
    processor = GeometryProcessor()
    dfm_executor = processor.dfm_executor

    overlay_paths: dict[str, str] = {}
    try:
        if stl_bytes is not None:
            # Fast path: use already-available STL bytes directly,
            # skipping the GLB file load entirely.
            import trimesh
            import io as _io

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
                storage_service,
                glb_bytes,
                overlay_path,
                "model/gltf-binary",
                upload_id,
            )
            if success:
                overlay_paths[overlay_key] = overlay_path
                logger.debug(f"Uploaded overlay: {overlay_key} → {overlay_paths[overlay_key]}")
            else:
                logger.warning(f"Failed to upload overlay: {overlay_key}")

        if overlay_glbs and not overlay_paths:
            logger.warning(
                "Generated %d overlay GLB(s) but 0 uploaded successfully for %s",
                len(overlay_glbs),
                upload_id,
            )
        elif not overlay_glbs and reports:
            logger.warning(
                "Generated 0 overlays despite non-empty reports (keys=%s) for %s",
                list(reports.keys()),
                upload_id,
            )
        else:
            logger.info(
                "Generated %d overlay(s) for DFM visualization (%d uploaded successfully)",
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
        try:
            processor.dfm_executor.shutdown(wait=True)
        except Exception:
            pass

    return overlay_paths


async def _upload_artifact(
    storage_service: IStorageService,
    data: bytes,
    path: str,
    content_type: str,
    upload_id: str,
) -> bool:
    """Upload an artifact to GCS.

    Args:
        storage_service: Storage service for uploading.
        data: Binary data to upload.
        path: GCS storage path.
        content_type: MIME content type.
        upload_id: Upload identifier for logging.

    Returns:
        True if upload succeeded, False otherwise.
    """
    import io

    try:
        file_stream = io.BytesIO(data)
        file_stream.seek(0)
        await storage_service.upload_file(file_stream, path, content_type)
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
