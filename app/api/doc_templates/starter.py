"""Starter offer template installed by the offer-setup defaults.

Deliberately plain: it uses only canonical merge fields and carries a visible
legal-review notice, so it cannot go live by accident. It is uploaded as a
draft version and still needs a second admin's approval (maker-checker).
"""
from __future__ import annotations

import io

from docx import Document
from docx.shared import Pt

STARTER_CODE = "STANDARD_FACILITY_LETTER"
STARTER_NAME = "Standard facility letter"
STARTER_FILE = "standard_facility_letter.docx"


def build_starter_docx() -> bytes:
    doc = Document()
    style = doc.styles["Normal"]
    style.font.size = Pt(11)

    doc.add_paragraph("{{ institution_name }}")
    doc.add_paragraph("{{ today }}")
    doc.add_paragraph("{{ borrower_full_name }}\n{{ borrower_address }}")
    doc.add_paragraph("Our reference: {{ consumer_ref }}")

    doc.add_heading("Facility letter — {{ product_label }}", level=1)
    doc.add_paragraph("Dear {{ borrower_full_name }},")
    doc.add_paragraph(
        "{{ institution_name }} is pleased to offer you the facility below, "
        "subject to the terms of this letter."
    )

    table = doc.add_table(rows=4, cols=2)
    rows = [
        ("Facility", "{{ product_label }}"),
        ("Amount", "{{ deal_amount_formatted }}"),
        ("Interest rate", "{{ deal_rate }}% per annum"),
        ("Term", "{{ deal_term_months }} months"),
    ]
    for (label, value), row in zip(rows, table.rows, strict=True):
        row.cells[0].text = label
        row.cells[1].text = value

    doc.add_heading("Terms and conditions", level=2)
    doc.add_paragraph(
        "[LEGAL REVIEW REQUIRED — replace this paragraph with your institution's "
        "approved standard terms before approving this version.]"
    )

    doc.add_heading("Acceptance", level=2)
    doc.add_paragraph(
        "By signing electronically, you accept this offer on the terms set out above."
    )
    doc.add_paragraph("For {{ institution_name }}")

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()
