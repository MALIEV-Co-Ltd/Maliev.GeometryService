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
) -> dict[str, str]:
    """Generate overlay GLBs and upload them to GCS.

    Args:
        glb_path: Path to the source GLB file.
        reports: DFM analysis reports dict (keyed by process code).
        storage_path: Base GCS storage path for the file (without extension).
        storage_service: Storage service for uploading artifacts.
        upload_id: Upload identifier for tracking.

    Returns:
        Dictionary mapping overlay keys (e.g. "FDM__thin_wall") to GCS paths.
    """
    import asyncio

    loop = asyncio.get_event_loop()

    # Import dfm_executor from geometry module for the executor
    from src.core.geometry import GeometryProcessor

    # Use a temporary executor for this one-off operation
    processor = GeometryProcessor()
    dfm_executor = processor.dfm_executor

    overlay_paths: dict[str, str] = {}
    try:
        # Generate overlay GLBs (returns dict[str, bytes])
        overlay_glbs: dict[str, bytes] = await loop.run_in_executor(
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
