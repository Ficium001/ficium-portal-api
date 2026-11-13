# =============================================================================
# ficium-portal-api — Offer setup: checklist + standard defaults
# Prefix: /institution/offer-setup
#
# A new institution used to face blank Committees / Workflow templates /
# Routing / Doc templates screens before it could send one offer. This gives
# it a checklist and a one-click install of standard defaults:
#
#   - a "Credit approval (standard)" chain for deals: one step, 'checker' role,
#     48h SLA, as the catch-all routing rule (priority 9999)
#   - a "Standard facility letter" Word template using only canonical merge
#     fields, with a visible legal-review notice in its terms section
#
# Dual control is kept, not bypassed: the chain is installed as
# pending_activation and the template as a draft version, so a DIFFERENT
# admin must activate/approve each before it can be used.
# =============================================================================
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..core.db import service_session
from ..core.roles import INSTITUTION_ADMIN_ROLES
from ..deps import current_claims, require_module, tenant_conn
from .doc_templates import starter
from .doc_templates.router import _member_id, _one, store_version

router = APIRouter(
    prefix="/institution/offer-setup",
    tags=["inst:doctemplates"],
    dependencies=[Depends(require_module("inst:doctemplates"))],
)

STANDARD_CHAIN = "Credit approval (standard)"


def _iid(claims: dict) -> str:
    iid = claims.get("institution_id")
    if not iid:
        raise HTTPException(status_code=403, detail="Not associated with an institution.")
    return str(iid)


def _item(key: str, label: str, state: str, detail: str, action: str | None = None,
          path: str | None = None) -> dict:
    return {"key": key, "label": label, "state": state, "detail": detail,
            "action": action, "path": path}


def setup_status(conn: Session, iid: str) -> dict:
    p = {"iid": iid}
    members = conn.execute(text("""
        SELECT count(*) AS n,
               count(*) FILTER (WHERE member_role = 'checker') AS checkers
        FROM institution.member WHERE institution_id = CAST(:iid AS uuid) AND active
    """), p).fetchone()
    chain = conn.execute(text("""
        SELECT t.status FROM institution.doa_rule r
        JOIN institution.approval_template t ON t.id = r.template_id
        WHERE r.institution_id = CAST(:iid AS uuid) AND r.entity_type = 'facility'
          AND r.status = 'active' AND r.priority = 9999
        LIMIT 1
    """), p).fetchone()
    tpl = conn.execute(text("""
        SELECT
          bool_or(t.status = 'active' AND t.current_version > 0) AS live,
          bool_or(EXISTS (SELECT 1 FROM institution.doc_template_version v
                          WHERE v.template_id = t.id
                            AND v.status IN ('draft', 'pending_approval'))) AS waiting
        FROM institution.doc_template t
        WHERE t.institution_id = CAST(:iid AS uuid) AND t.status <> 'retired'
          AND t.doc_category IN ('facility_letter', 'loan_agreement')
    """), p).fetchone()

    n = members.n if members else 0
    checkers = members.checkers if members else 0
    items = [
        _item("second_person", "A second team member", "done" if n >= 2 else "todo",
              "Two people are needed: whoever sets something up can't also approve it."
              if n < 2 else f"{n} active team members.",
              None if n >= 2 else "Invite a colleague", "/setup?section=team"),
        _item("checker", "Someone who approves credit", "done" if checkers else "todo",
              f"{checkers} member(s) with the checker role." if checkers else
              "The standard credit chain is approved by a member with the checker role.",
              None if checkers else "Give someone the checker role", "/setup?section=team"),
    ]

    if chain is None:
        items.append(_item("credit_chain", "Credit approval chain", "todo",
                           "No catch-all credit approval chain yet.", "install"))
    elif chain.status == "active":
        items.append(_item("credit_chain", "Credit approval chain", "done", "Active."))
    elif chain.status == "pending_activation":
        items.append(_item("credit_chain", "Credit approval chain", "waiting",
                           "Installed. A different admin must activate it.",
                           "Review and activate", "/setup?section=credit-approval"))
    else:
        items.append(_item("credit_chain", "Credit approval chain", "todo",
                           f"The chain is {chain.status}. Submit it for activation.",
                           "Open credit approval", "/setup?section=credit-approval"))

    if tpl and tpl.live:
        items.append(_item("offer_template", "Offer letter template", "done", "Approved and live."))
    elif tpl and tpl.waiting:
        items.append(_item("offer_template", "Offer letter template", "waiting",
                           "Uploaded. A different admin must review the wording and approve it.",
                           "Review template", "/setup?section=documents"))
    else:
        items.append(_item("offer_template", "Offer letter template", "todo",
                           "No offer template yet.", "install"))

    return {
        "complete": all(i["state"] == "done" for i in items),
        "can_install": any(i["action"] == "install" for i in items),
        "items": items,
    }


@router.get("/status")
async def get_status(
    conn: Session = Depends(tenant_conn), claims: dict = Depends(current_claims),
) -> dict:
    return setup_status(conn, _iid(claims))


@router.post("/install")
async def install_defaults(
    conn: Session = Depends(tenant_conn), claims: dict = Depends(current_claims),
) -> dict:
    """Idempotent: installs only what is missing; never touches existing setup."""
    if claims.get("user_role") not in INSTITUTION_ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Only an institution admin can install defaults.")
    iid = _iid(claims)

    has_catch_all = conn.execute(text("""
        SELECT 1 FROM institution.doa_rule
        WHERE institution_id = CAST(:iid AS uuid) AND entity_type = 'facility'
          AND priority = 9999
    """), {"iid": iid}).fetchone()
    if has_catch_all is None:
        # Same ids the approval engine stores for templates (claims["sub"]), so
        # its activate step's maker != checker comparison holds.
        tpl = conn.execute(text("""
            INSERT INTO institution.approval_template
              (institution_id, name, entity_type, status, created_by)
            VALUES (CAST(:iid AS uuid), :name, 'facility', 'pending_activation', :uid)
            ON CONFLICT (institution_id, name, version) DO NOTHING
            RETURNING id
        """), {"iid": iid, "name": STANDARD_CHAIN, "uid": claims["sub"]}).fetchone()
        if tpl is not None:
            conn.execute(text("""
                INSERT INTO institution.approval_stage_def
                  (template_id, seq, name, stage_type, approver_role, sla_hours, on_sla_breach)
                VALUES (:tid, 1, 'Credit approval', 'single', 'checker', 48, 'notify')
            """), {"tid": tpl.id})
            conn.execute(text("""
                INSERT INTO institution.doa_rule
                  (institution_id, entity_type, priority, conditions, template_id, created_by)
                VALUES (CAST(:iid AS uuid), 'facility', 9999, '{}'::jsonb, :tid, :uid)
            """), {"iid": iid, "tid": tpl.id, "uid": claims["sub"]})
        conn.commit()

    has_offer_template = conn.execute(text("""
        SELECT 1 FROM institution.doc_template
        WHERE institution_id = CAST(:iid AS uuid) AND status <> 'retired'
          AND (doc_category IN ('facility_letter', 'loan_agreement') OR code = :code)
    """), {"iid": iid, "code": starter.STARTER_CODE}).fetchone()
    if has_offer_template is None:
        with service_session() as s:
            creator = _member_id(s, claims)
            template = _one(s.execute(text("""
                INSERT INTO institution.doc_template
                    (institution_id, code, name, description, doc_category, created_by)
                VALUES (:iid, :code, :name, :desc, 'facility_letter', :creator)
                RETURNING *
            """), {
                "iid": iid, "code": starter.STARTER_CODE, "name": starter.STARTER_NAME,
                "desc": "Starter template. Replace the terms section with your approved wording.",
                "creator": creator,
            }))
            if template is None:
                raise HTTPException(status_code=500, detail="Could not create the template.")
            await store_version(
                s, claims=claims, institution_id=iid, template=template, creator_id=creator,
                file_name=starter.STARTER_FILE, data=starter.build_starter_docx(),
                change_note="Starter template. Legal review of the terms section required.",
            )
            s.commit()

    return setup_status(conn, iid)
