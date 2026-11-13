"""
Server-side gate for sending a deal's documents for signature.

Before: the approval gate in institution.esign_create_envelope() applied only
when the CLIENT labelled the envelope 'offer_letter' or 'investment_mandate',
and the only way to supply an approval was a preset that no screen ever passed.
So an offer letter sent from a deal always failed with a vague 409, and the same
document labelled "Other" (or a loan agreement, which mapped to 'custom') went
out with no approval at all.

Now the server decides: any envelope raised against a deal needs that deal's
approved credit decision (an approved 'facility' instance, or an approved
'offer_letter' instance where an institution runs one). The client's label no
longer opens or closes the gate. When the gate is shut, the message names the
step that is pending and who has it.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

# Template category -> e-sign envelope type. Anything that binds the bank to
# lend is an offer. Deal envelopes are gated regardless; this is the label.
ENVELOPE_TYPE_FOR_CATEGORY: dict[str, str] = {
    "facility_letter": "offer_letter",
    "loan_agreement": "offer_letter",
    "legal_agreement": "offer_letter",
    "terms_conditions": "custom",
    "other": "custom",
}


@dataclass
class ApprovalState:
    state: str  # approved | pending | rejected | none
    instance_id: str | None
    message: str

    @property
    def approved(self) -> bool:
        return self.state == "approved"


def is_deal(conn: Session, institution_id: str, entity_id: str) -> bool:
    row = conn.execute(text("""
        SELECT 1 FROM marketplace.loan_pipeline
        WHERE id = CAST(:id AS uuid) AND institution_id = CAST(:iid AS uuid)
    """), {"id": str(entity_id), "iid": str(institution_id)}).fetchone()
    return row is not None


def _when(ts: datetime | None) -> str:
    return ts.strftime("%d %b %Y %H:%M UTC") if ts else ""


def _approver_text(conn: Session, institution_id: str, stage) -> str:
    if stage.committee_name:
        return f"the {stage.committee_name} committee"
    role = stage.approver_role
    if not role:
        return "an eligible approver"
    names = [r.full_name for r in conn.execute(text("""
        SELECT full_name FROM institution.member
        WHERE institution_id = CAST(:iid AS uuid) AND active AND member_role = :role
          AND full_name IS NOT NULL
        ORDER BY full_name LIMIT 3
    """), {"iid": str(institution_id), "role": role}).fetchall()]
    if not names:
        return (f"a member with the '{role}' role — nobody on your team has that "
                f"role yet, so add one under Team")
    return f"{' or '.join(names)} ('{role}' role)"


def deal_approval(conn: Session, institution_id: str, pipeline_id: str) -> ApprovalState:
    """The deal's credit decision as it stands, with a message naming any
    pending step and its approver."""
    inst = conn.execute(text("""
        SELECT id, status, resolved_at
        FROM institution.approval_instance
        WHERE institution_id = CAST(:iid AS uuid) AND entity_id = CAST(:pid AS uuid)
          AND entity_type IN ('facility', 'offer_letter')
        ORDER BY (status = 'approved') DESC, started_at DESC
        LIMIT 1
    """), {"iid": str(institution_id), "pid": str(pipeline_id)}).fetchone()

    if inst is None:
        return ApprovalState("none", None, (
            "This deal has no credit approval yet. Submit it for credit approval "
            "on the deal's credit stage, then send the offer."
        ))

    if inst.status == "approved":
        return ApprovalState("approved", str(inst.id), "Credit approval granted.")

    if inst.status == "in_progress":
        stage = conn.execute(text("""
            SELECT si.seq, si.due_at, sd.name, sd.approver_role,
                   c.name AS committee_name
            FROM institution.approval_stage_instance si
            JOIN institution.approval_stage_def sd ON sd.id = si.stage_def_id
            LEFT JOIN institution.approval_committee c ON c.id = sd.committee_id
            WHERE si.instance_id = :id AND si.status = 'active'
            ORDER BY si.seq LIMIT 1
        """), {"id": inst.id}).fetchone()
        if stage is None:
            return ApprovalState("pending", str(inst.id),
                                 "Credit approval is in progress. Try again shortly.")
        due = f", due {_when(stage.due_at)}" if stage.due_at else ""
        return ApprovalState("pending", str(inst.id), (
            f"Waiting for credit approval: step {stage.seq} \"{stage.name}\" is with "
            f"{_approver_text(conn, institution_id, stage)}{due}."
        ))

    when = f" on {_when(inst.resolved_at)}" if inst.resolved_at else ""
    return ApprovalState("rejected", str(inst.id), (
        f"Credit approval was {inst.status}{when}. Resubmit the deal for credit "
        "approval before sending documents."
    ))


def envelope_type_for_generation(
    conn: Session, institution_id: str, generation_id: str | None, document_path: str,
) -> str | None:
    """Envelope type implied by the template a generated document came from.
    Matches on the generation id or, failing that, its output path."""
    row = conn.execute(text("""
        SELECT t.doc_category
        FROM institution.doc_generation g
        JOIN institution.doc_template t ON t.id = g.template_id
        WHERE g.institution_id = CAST(:iid AS uuid)
          AND (g.id::text = :gid OR g.output_pdf_path = :path OR g.output_docx_path = :path)
        LIMIT 1
    """), {"iid": str(institution_id), "gid": str(generation_id or ""),
           "path": document_path}).fetchone()
    if row is None:
        return None
    return ENVELOPE_TYPE_FOR_CATEGORY.get(row.doc_category, "custom")


def deal_borrower(conn: Session, institution_id: str, pipeline_id: str) -> dict:
    """Borrower identity revealed on bid acceptance. Read server-side so the
    signing link goes to the identity the deal holds, not to typed text."""
    row = conn.execute(text("""
        SELECT ba.full_name, ba.email, cp.label AS product_label,
               UPPER(LEFT(lp.consumer_id::text, 8)) AS consumer_ref
        FROM marketplace.loan_pipeline lp
        LEFT JOIN marketplace.request r ON r.id = lp.request_id
        LEFT JOIN catalog.product cp ON cp.id = r.product_id
        LEFT JOIN marketplace.bid_acceptance ba
               ON ba.request_id = lp.request_id AND ba.institution_id = lp.institution_id
        WHERE lp.id = CAST(:pid AS uuid) AND lp.institution_id = CAST(:iid AS uuid)
    """), {"pid": str(pipeline_id), "iid": str(institution_id)}).fetchone()
    if row is None:
        return {}
    return {
        "name": row.full_name or "",
        "email": row.email or "",
        "product_label": row.product_label or "",
        "consumer_ref": row.consumer_ref or "",
    }
