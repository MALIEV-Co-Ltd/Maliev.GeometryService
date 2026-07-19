"""Stable identity helpers for GeometryService lifecycle events."""

from uuid import NAMESPACE_URL, UUID, uuid5


def resolve_geometry_correlation_id(
    correlation_id: UUID | None,
    file_id: str,
    storage_path: str,
) -> UUID:
    """Return the supplied correlation ID or derive one from canonical file identity."""
    canonical_file_id = file_id.strip()
    canonical_storage_path = storage_path.strip()
    if not canonical_file_id:
        raise ValueError("file_id is required to correlate geometry events")
    if not canonical_storage_path:
        raise ValueError("storage_path is required to correlate geometry events")
    if correlation_id is not None and correlation_id.int != 0:
        return correlation_id

    return uuid5(
        NAMESPACE_URL,
        f"maliev.geometry:{canonical_file_id}\n{canonical_storage_path}",
    )
