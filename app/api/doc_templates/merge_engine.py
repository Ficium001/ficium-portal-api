"""inst:doctemplates — merge engine.

Populates a .docx template (Jinja2-in-docx via docxtpl) with a data snapshot
and optionally converts the result to PDF via LibreOffice headless.

Design notes:
- Templates are stored as-is (author authoring in Word with {{ field }} tags).
- merge_field_map on the version documents which fields the template expects
  and where each maps from in the deal data (dot path), purely for the
  template-designer UI to validate/preview — the actual substitution at
  generation time uses whatever keys exist in the resolved context.
- Never trust template content as executable beyond Jinja2 expression syntax
  that docxtpl itself parses; no custom Jinja filters are registered, so
  templates cannot reach outside the provided context.
"""
from __future__ import annotations

import hashlib
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from docxtpl import DocxTemplate


class MergeError(RuntimeError):
    pass


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def render_docx(template_path: Path, context: dict[str, Any], out_path: Path) -> None:
    """Render a docxtpl template with the given context to out_path."""
    try:
        tpl = DocxTemplate(str(template_path))
        tpl.render(context)
        tpl.save(str(out_path))
    except Exception as exc:  # docxtpl raises plain Exception/jinja2 errors
        raise MergeError(f"template render failed: {exc}") from exc


def convert_to_pdf(docx_path: Path, out_dir: Path, timeout_s: int = 60) -> Path:
    """Convert a .docx to .pdf using headless LibreOffice. Returns the pdf path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [
            "soffice", "--headless", "--norestore",
            "--convert-to", "pdf", "--outdir", str(out_dir), str(docx_path),
        ],
        capture_output=True, timeout=timeout_s,
    )
    if proc.returncode != 0:
        raise MergeError(f"pdf conversion failed: {proc.stderr.decode(errors='ignore')}")
    pdf_path = out_dir / (docx_path.stem + ".pdf")
    if not pdf_path.exists():
        raise MergeError("pdf conversion reported success but output file is missing")
    return pdf_path


def generate(
    template_path: Path,
    context: dict[str, Any],
    work_dir: Path,
    base_name: str,
    make_pdf: bool = True,
) -> tuple[Path, Path | None]:
    """Full pipeline: render docx, optionally convert to pdf. Returns (docx_path, pdf_path)."""
    work_dir.mkdir(parents=True, exist_ok=True)
    docx_out = work_dir / f"{base_name}.docx"
    render_docx(template_path, context, docx_out)
    pdf_out = convert_to_pdf(docx_out, work_dir) if make_pdf else None
    return docx_out, pdf_out


def resolve_context(entity_snapshot: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """Merge auto-resolved deal data with explicit overrides (overrides win)."""
    ctx = dict(entity_snapshot)
    ctx.update(overrides or {})
    return ctx
