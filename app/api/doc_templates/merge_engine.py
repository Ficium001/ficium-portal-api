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


# ---------------------------------------------------------------------------
# Upload-time validation
# ---------------------------------------------------------------------------
# A merge-tag typo used to surface only at generation time ("check the template
# for typos"), after a second admin had already approved the version. Checking
# at upload means the maker fixes it before it ever reaches the checker.

class TemplateCheck:
    def __init__(
        self,
        used: list[str],
        unknown: list[tuple[str, str | None]],
        error: str | None = None,
    ) -> None:
        self.used = used
        self.unknown = unknown  # (tag as written, closest known field or None)
        self.error = error

    @property
    def ok(self) -> bool:
        return not self.unknown and self.error is None

    def message(self) -> str:
        if self.error:
            return self.error
        lines = ["This template can't be uploaded yet:"]
        for tag, suggestion in self.unknown:
            hint = f" — did you mean {{{{ {suggestion} }}}}?" if suggestion else ""
            lines.append(f"• {{{{ {tag} }}}} is not a merge field{hint}")
        lines.append(
            "Fix these in Word and upload again. The merge-field list on this "
            "page shows every field you can use."
        )
        return "\n".join(lines)


def check_template(template_path: Path, known_fields: dict[str, str]) -> TemplateCheck:
    """Check a .docx template's tags against the known merge fields, then
    test-fill it with example values. Returns problems instead of raising."""
    import difflib
    import tempfile

    from jinja2 import TemplateSyntaxError

    try:
        names = DocxTemplate(str(template_path)).get_undeclared_template_variables()
    except TemplateSyntaxError as exc:
        return TemplateCheck([], [], (
            f"A merge tag in this Word file is broken ({exc.message}). "
            "Write every tag exactly as {{ field_name }} and upload again."
        ))
    except Exception:  # noqa: BLE001 — not a readable .docx at all
        return TemplateCheck([], [], (
            "This file could not be read as a Word template. "
            "Save it from Word as .docx and upload again."
        ))

    used = sorted(n for n in names if n in known_fields)
    unknown = [
        (n, next(iter(difflib.get_close_matches(n, list(known_fields), n=1, cutoff=0.6)), None))
        for n in sorted(n for n in names if n not in known_fields)
    ]
    if unknown:
        return TemplateCheck(used, unknown)

    try:
        with tempfile.TemporaryDirectory() as tmp:
            render_docx(template_path, dict(known_fields), Path(tmp) / "check.docx")
    except MergeError:
        return TemplateCheck(used, [], (
            "This template failed a test fill with example values. Check that "
            "tags only use {{ field_name }} with no extra logic, then upload again."
        ))
    return TemplateCheck(used, [])
