from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest

from src.core.event_identity import resolve_geometry_correlation_id


@pytest.mark.parametrize(
    ("file_id", "storage_path"),
    [
        ("", "projects/file.step"),
        ("   ", "projects/file.step"),
        ("file-123", ""),
        ("file-123", "   "),
    ],
)
def test_supplied_correlation_cannot_bypass_canonical_identity_validation(
    file_id: str,
    storage_path: str,
) -> None:
    with pytest.raises(ValueError):
        resolve_geometry_correlation_id(uuid4(), file_id, storage_path)


def test_supplied_correlation_is_preserved_for_valid_canonical_identity() -> None:
    correlation_id = uuid4()

    resolved = resolve_geometry_correlation_id(
        correlation_id,
        "file-123",
        "projects/file.step",
    )

    assert resolved == correlation_id


def test_empty_uuid_is_replaced_with_deterministic_canonical_correlation() -> None:
    resolved = resolve_geometry_correlation_id(
        UUID(int=0),
        "file-123",
        "projects/file.step",
    )

    assert resolved == uuid5(
        NAMESPACE_URL,
        "maliev.geometry:file-123\nprojects/file.step",
    )
