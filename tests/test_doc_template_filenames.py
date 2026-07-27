# =============================================================================
# Unit tests — _safe_filename() in app/api/doc_templates/router.py
#
# REGRESSION TESTS. UploadFile.filename is client-controlled and Optional, and
# was previously interpolated raw into both a filesystem path and the object
# storage key. Two consequences:
#   * Path(tmp) / "../../x"      escapes the temp directory
#   * Path(tmp) / "/etc/passwd"  discards the base path entirely
# and because the storage key carries the per-institution prefix that keeps
# tenants apart, traversal there crossed a tenant boundary, not just a folder.
# =============================================================================

from __future__ import annotations

from pathlib import Path

import pytest

from app.api.doc_templates.router import _safe_filename


@pytest.mark.parametrize(
    "supplied",
    [
        "../../../../etc/passwd",
        "../../secrets.docx",
        "/etc/passwd",
        "/absolute/path/agreement.docx",
        "..\\..\\windows\\system32\\config",
        "....//....//escape.docx",
    ],
)
def test_traversal_attempts_are_reduced_to_a_bare_basename(supplied: str) -> None:
    safe = _safe_filename(supplied)
    assert "/" not in safe
    assert "\\" not in safe
    assert not safe.startswith("..")


@pytest.mark.parametrize(
    "supplied",
    [
        "../../../../etc/passwd",
        "/etc/passwd",
        "../escape.docx",
    ],
)
def test_joined_path_stays_inside_the_base_directory(supplied: str, tmp_path: Path) -> None:
    """The property that actually matters: the result cannot escape the base."""
    joined = (tmp_path / _safe_filename(supplied)).resolve()
    assert joined.is_relative_to(tmp_path.resolve())


def test_none_filename_falls_back_to_a_default() -> None:
    # UploadFile.filename is Optional; this used to raise TypeError on join.
    assert _safe_filename(None) == "upload.docx"


@pytest.mark.parametrize("supplied", ["", "   ", ".", "..", "/", "../"])
def test_empty_or_degenerate_names_fall_back_to_a_default(supplied: str) -> None:
    assert _safe_filename(supplied) == "upload.docx"


def test_ordinary_filenames_are_preserved() -> None:
    assert _safe_filename("Loan Agreement v3.docx") == "Loan Agreement v3.docx"
    assert _safe_filename("facility_letter.docx") == "facility_letter.docx"


def test_storage_key_prefix_cannot_be_escaped() -> None:
    """
    Guards the tenant boundary specifically: the institution id segment must
    remain the parent of whatever the client supplied.
    """
    key = f"doc-templates/inst-a/tpl-1/v2/{_safe_filename('../../inst-b/steal.docx')}"
    assert key.startswith("doc-templates/inst-a/tpl-1/v2/")
    assert "inst-b" not in key.replace("steal.docx", "")
