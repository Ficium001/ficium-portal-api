# =============================================================================
# ficium-portal-api — Send offer for signature (one step from a deal)
# Prefix: /institution/deals
#
# Replaces a four-screen routine (generate under Documents, find the approval,
# open the envelope form, retype the borrower) with one call:
#
#   GET  /{pipeline_id}/offer-readiness  what's ready, what's blocking, defaults
#   POST /{pipeline_id}/send-offer       generate from template + send envelope
#
# Nothing here weakens a control. It composes the existing pieces: generation
# goes through doc_templates.generate_document (published versions only), and
# the envelope goes through esign.create_envelope, which re-checks the credit
# approval gate itself. Borrower and countersigner identities are read
# server-side, never taken from the client.
# =============================================================================
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..core import offer_gate
from ..deps import current_claims, require_module, tenant_conn
from . import esign
from .doc_templates.router import generate_document
from .doc_templates.schemas import GenerateRequest

router = APIRouter(
    prefix="/institution/deals",
    tags=["inst:esign"],
    dependencies=[Depends(require_module("inst:doctemplates"))],
)

SIGNING_WINDOWS_HOURS = (24, 48, 72, 168, 336)


def _iid(claims: dict) -> str:
    iid = claims.get("institution_id")
    if not iid:
        raise HTTPException(status_code=403, detail="Not associated with an institution.")
    return str(iid)


def _templates(conn: Session, iid: str) -> list[dict]:
    rows = conn.execute(text("""
        SELECT id, name, doc_category, current_version
        FROM institution.doc_template
        WHERE institution_id = CAST(:iid AS uuid) AND status = 'active'
          AND current_version > 0
        ORDER BY CASE doc_category WHEN 'facility_letter' THEN 0
                                   WHEN 'loan_agreement'  THEN 1 ELSE 2 END, name
    """), {"iid": iid}).fetchall()
    return [
        {"id": str(r.id), "name": r.name, "doc_category": r.doc_category,
         "envelope_type": offer_gate.ENVELOPE_TYPE_FOR_CATEGORY.get(r.doc_category, "custom")}
        for r in rows
    ]


def _readiness(conn: Session, iid: str, pipeline_id: str) -> dict:
    if not offer_gate.is_deal(conn, iid, pipeline_id):
        raise HTTPException(status_code=404, detail="Deal not found.")

    approval = offer_gate.deal_approval(conn, iid, pipeline_id)
    borrower = offer_gate.deal_borrower(conn, iid, pipeline_id)
    templates = _templates(conn, iid)
    live = conn.execute(text("""
        SELECT count(*) AS n FROM institution.esign_envelope
        WHERE institution_id = CAST(:iid AS uuid) AND entity_id = CAST(:pid AS uuid)
          AND status IN ('sent', 'partially_signed', 'completed')
    """), {"iid": iid, "pid": pipeline_id}).fetchone()

    blockers: list[str] = []
    if not approval.approved:
        blockers.append(approval.message)
    if not borrower.get("name") or not borrower.get("email"):
        blockers.append(
            "The borrower's name and email are not on this deal yet. They are "
            "revealed when the borrower accepts your bid."
        )
    if not templates:
        blockers.append(
            "There is no approved offer template. Install the standard one, or "
            "upload your own, under Setup › Document templates."
        )

    return {
        "ready": not blockers,
        "blockers": blockers,
        "approval": {"state": approval.state, "message": approval.message},
        "borrower": {"name": borrower.get("name", ""), "email": borrower.get("email", "")},
        "templates": templates,
        "default_template_id": templates[0]["id"] if templates else None,
        "already_sent": int(live.n if live else 0),
        "deal_label": " — ".join(
            x for x in (borrower.get("product_label"),
                        f"ref {borrower['consumer_ref']}" if borrower.get("consumer_ref") else "")
            if x),
    }


@router.get("/{pipeline_id}/offer-readiness")
async def offer_readiness(
    pipeline_id: UUID,
    conn: Session = Depends(tenant_conn),
    claims: dict = Depends(current_claims),
) -> dict:
    esign._require_module(claims)
    return _readiness(conn, _iid(claims), str(pipeline_id))


@router.post("/{pipeline_id}/send-offer")
async def send_offer(
    pipeline_id: UUID,
    body: dict = Body(...),
    conn: Session = Depends(tenant_conn),
    claims: dict = Depends(current_claims),
) -> dict:
    """body: { template_id, countersigner_member_id, expires_hours? }"""
    esign._require_module(claims)
    iid, pid = _iid(claims), str(pipeline_id)

    state = _readiness(conn, iid, pid)
    if not state["ready"]:
        raise HTTPException(status_code=409, detail=state["blockers"][0])

    template = next((t for t in state["templates"] if t["id"] == str(body.get("template_id"))), None)
    if template is None:
        raise HTTPException(status_code=422, detail="Choose an approved template.")

    signer = None
    if body.get("countersigner_member_id"):
        signer = conn.execute(text("""
            SELECT id, full_name, email FROM institution.member
            WHERE id = CAST(:mid AS uuid) AND institution_id = CAST(:iid AS uuid) AND active
        """), {"mid": str(body["countersigner_member_id"]), "iid": iid}).fetchone()
    if signer is None or not signer.email:
        raise HTTPException(status_code=422,
                            detail="Choose an active team member with an email to countersign.")

    expires = int(body.get("expires_hours") or 72)
    if expires not in SIGNING_WINDOWS_HOURS:
        raise HTTPException(status_code=422, detail="Signing window must be 1, 2, 3, 7 or 14 days.")

    gen = await generate_document(
        UUID(template["id"]),
        GenerateRequest(entity_type="loan_pipeline", entity_id=pipeline_id, output_pdf=True),
        claims,
    )
    if gen["status"] != "generated":
        raise HTTPException(
            status_code=502,
            detail="The offer document could not be generated, so nothing was sent. "
                   "Check the template under Setup › Document templates and try again.",
        )

    borrower = state["borrower"]
    result = await esign.create_envelope(
        body={
            "entity_type": template["envelope_type"],
            "entity_id": pid,
            "title": f"{template['name']} — {state['deal_label']}" if state["deal_label"]
                     else template["name"],
            "document_path": gen["output_pdf_path"] or gen["output_docx_path"],
            "doc_generation_id": str(gen["id"]),
            "expires_hours": expires,
            "borrower_name": borrower["name"],
            "borrower_email": borrower["email"],
            "countersigner_name": signer.full_name or signer.email,
            "countersigner_email": signer.email,
            "countersigner_ref": str(signer.id),
        },
        conn=conn,
        claims=claims,
    )
    return {"envelope_id": result["envelope_id"], "generation_id": str(gen["id"])}
