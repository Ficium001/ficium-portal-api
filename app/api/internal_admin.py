"""
Admin command channel: the ONLY way the portal admin site reads or changes portal data.

The admin site has its own database and never connects to this one. It calls two
endpoints here, both POST with a JSON body:

    POST /internal/admin/v1/query     read-only ops (one row set per call)
    POST /internal/admin/v1/commands  changes; idempotent by command_id

Ops: compliance.overview, documents.list, document.download, institutions.list,
entitlements.catalog, groups.list, audit.search, access_requests.list (queries);
document.review, access_request.review (one person); waiver.grant, waiver.revoke,
institution.approve, institution.suspend, entitlement.set, institution.provision,
institution.invite_admin (four-eyes).

Every call must pass BOTH checks, so neither a leaked signing key nor a stolen
staff token is enough on its own:

  1. Ficium-Signature: HMAC-SHA256 over the raw body (same scheme and 300 s window
     as the integration contract), with a key only the admin site's backend holds.
     The op is inside the signed body, so a captured call cannot be re-aimed.
  2. X-Ficium-Actor: the acting staff member's ficium-auth access token. Must be
     from the Ficium platform institution, a staff role, and signed in WITH MFA.

Four-eyes is decided in the admin database; this side re-checks what it can see:
for four-eyes ops the maker must differ from the actor (the approver). Every
command writes an audit.event and an append-only integration.admin_command_log
row; replaying a command_id returns the stored result and changes nothing.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import ficium_contract as fc
import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from ..core.config import settings
from ..core.db import service_session
from ..core.security import TokenError, verify_token

log = structlog.get_logger()
router = APIRouter(prefix="/internal/admin/v1", tags=["internal-admin"], include_in_schema=False)

MAX_BODY = 64_000
ACTOR_HEADER = "X-Ficium-Actor"
FOUR_EYES_OPS = frozenset({
    "waiver.grant", "waiver.revoke",
    "institution.approve", "institution.suspend", "entitlement.set",
    "commission.set", "commission.end",
    "institution.provision", "institution.invite_admin",
})
MAX_WAIVER_DAYS = 180


class ChannelError(Exception):
    def __init__(self, status: int, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.status, self.code, self.detail = status, code, detail


def _err(status: int, code: str, detail: str = "") -> JSONResponse:
    body: dict[str, Any] = {"error": code}
    if detail:
        body["detail"] = detail
    return JSONResponse(status_code=status, content=body)


def _jsonable(v: Any) -> Any:
    return json.loads(json.dumps(v, default=str))


def _uuid(v: Any, field: str) -> str:
    try:
        return str(uuid.UUID(str(v)))
    except (ValueError, TypeError) as e:
        raise ChannelError(422, "invalid_field", f"{field} must be a UUID") from e


def _reason(v: Any, minimum: int = 10) -> str:
    r = str(v or "").strip()
    if not minimum <= len(r) <= 500:
        raise ChannelError(422, "invalid_field", f"reason must be {minimum}-500 characters")
    return r


# ── Authentication ────────────────────────────────────────────────────────────

async def _authenticate(request: Request) -> tuple[bytes, dict[str, Any], dict[str, Any]]:
    keys = settings.admin_command_keys
    if not keys:
        raise ChannelError(503, "admin_channel_disabled")
    raw = await request.body()
    if len(raw) > MAX_BODY:
        raise ChannelError(413, "body_too_large")
    try:
        fc.verify(request.headers.get(fc.SIGNATURE_HEADER, ""), raw, keys)
    except fc.ContractError as e:
        log.warning("admin_channel_bad_signature", reason=str(e))
        raise ChannelError(401, "bad_signature") from e

    token = request.headers.get(ACTOR_HEADER, "")
    if not token:
        raise ChannelError(401, "actor_required")
    try:
        claims = await verify_token(token)
    except TokenError as e:
        log.warning("admin_channel_bad_actor", reason=str(e))
        raise ChannelError(401, "bad_actor") from e
    if str(claims.get("institution_id")) != settings.platform_institution_id:
        raise ChannelError(403, "not_staff")
    if str(claims.get("user_role", "")).lower() not in settings.admin_staff_role_set:
        raise ChannelError(403, "not_staff")
    amr = claims.get("amr")
    if not isinstance(amr, list) or "mfa" not in amr:
        raise ChannelError(403, "mfa_required")
    claims["sub"] = _uuid(claims.get("sub"), "actor sub")

    try:
        body = json.loads(raw)
    except ValueError as e:
        raise ChannelError(422, "invalid_json") from e
    if not isinstance(body, dict) or not isinstance(body.get("op"), str):
        raise ChannelError(422, "op_required")
    args = body.get("args", {})
    if not isinstance(args, dict):
        raise ChannelError(422, "invalid_field", "args must be an object")
    body["args"] = args
    return raw, body, claims


def _audit(conn: Session, actor: str, action: str, resource_type: str, resource_id: str | None,
           institution_id: str | None, meta: dict[str, Any], outcome: str = "success") -> None:
    conn.execute(
        text("""
            INSERT INTO audit.event (actor_id, actor_type, institution_id, action,
                                     resource_type, resource_id, outcome, metadata)
            VALUES (CAST(:actor AS uuid), 'admin', CAST(:iid AS uuid), :action,
                    :rtype, CAST(:rid AS uuid), :outcome, CAST(:meta AS jsonb))
        """),
        {"actor": actor, "iid": institution_id, "action": action, "rtype": resource_type,
         "rid": resource_id, "outcome": outcome,
         "meta": json.dumps({**meta, "via": "admin-portal"}, default=str)},
    )


# ── Queries ───────────────────────────────────────────────────────────────────

def _q_compliance_overview(conn: Session, args: dict, actor: str) -> list[dict]:
    rows = conn.execute(text("""
        SELECT i.id AS institution_id, i.name, i.institution_type, i.onboarding_stage,
               c.is_approved, c.is_compliant, c.can_bid, c.missing_docs, c.waived_until,
               w.reason AS waiver_reason, w.granted_at AS waiver_granted_at,
               w.revoked_at AS waiver_revoked_at,
               (SELECT count(*) FROM institution.doc d
                 WHERE d.institution_id = i.id AND d.status = 'pending') AS docs_pending
          FROM institution.institution i
          JOIN institution.compliance c ON c.institution_id = i.id
          LEFT JOIN institution.compliance_waiver w ON w.institution_id = i.id
         ORDER BY c.can_bid, i.name
         LIMIT 500
    """)).fetchall()
    out = []
    for r in rows:
        d = dict(r._mapping)
        d["missing_docs"] = list(d["missing_docs"] or [])
        out.append(d)
    return out


def _q_documents_list(conn: Session, args: dict, actor: str) -> list[dict]:
    iid = _uuid(args["institution_id"], "institution_id") if args.get("institution_id") else None
    status = args.get("status")
    if status is not None and status not in ("pending", "approved", "rejected", "expired"):
        raise ChannelError(422, "invalid_field", "status is not valid")
    rows = conn.execute(text("""
        SELECT d.id, d.institution_id, i.name AS institution_name,
               dt.code AS doc_type_code, dt.label AS doc_type_label, dt.is_mandatory,
               d.file_name, d.mime_type, d.status, d.expiry_date, d.rejection_reason,
               d.uploaded_at, d.reviewed_at, d.reviewed_by_staff
          FROM institution.doc d
          JOIN institution.institution i ON i.id = d.institution_id
          JOIN institution.doc_type dt   ON dt.id = d.doc_type_id
         WHERE (CAST(:iid AS uuid) IS NULL OR d.institution_id = CAST(:iid AS uuid))
           AND (CAST(:status AS text) IS NULL OR d.status = :status)
         ORDER BY CASE d.status WHEN 'pending' THEN 0 WHEN 'rejected' THEN 1 ELSE 2 END,
                  d.uploaded_at DESC
         LIMIT 500
    """), {"iid": iid, "status": status}).fetchall()
    return [dict(r._mapping) for r in rows]


async def _q_document_download(conn: Session, args: dict, actor: str) -> dict:
    from ..core.storage import storage_signed_url

    doc_id = _uuid(args.get("doc_id"), "doc_id")
    row = conn.execute(text(
        "SELECT institution_id, storage_path, file_name FROM institution.doc WHERE id = CAST(:id AS uuid)"
    ), {"id": doc_id}).fetchone()
    if row is None or not row.storage_path:
        raise ChannelError(404, "not_found", "Document not found.")
    # Viewing a bank's compliance document is itself recorded.
    _audit(conn, actor, "compliance.document_viewed", "institution_doc", doc_id,
           str(row.institution_id), {})
    return {"url": await storage_signed_url(row.storage_path, 300), "file_name": row.file_name}


def _q_institutions_list(conn: Session, args: dict, actor: str) -> list[dict]:
    rows = conn.execute(text("""
        SELECT i.id AS institution_id, i.name, i.legal_name, i.institution_type, i.country,
               i.onboarding_stage, i.approved, i.approved_at, i.suspended_at, i.suspension_reason,
               i.primary_contact_name, i.primary_contact_email, i.created_at,
               (SELECT count(*) FROM institution.member m
                 WHERE m.institution_id = i.id AND m.deactivated_at IS NULL) AS active_members,
               COALESCE((SELECT jsonb_agg(jsonb_build_object(
                          'module_code', e.module_code, 'status', e.status, 'plan_tier', e.plan_tier,
                          'valid_to', e.valid_to) ORDER BY e.module_code)
                   FROM entitlements.institution_entitlement e
                  WHERE e.institution_id = i.id), '[]'::jsonb) AS entitlements
          FROM institution.institution i
         ORDER BY i.approved, i.name
         LIMIT 500
    """)).fetchall()
    return [dict(r._mapping) for r in rows]


def _q_entitlements_catalog(conn: Session, args: dict, actor: str) -> list[dict]:
    rows = conn.execute(text("""
        SELECT module_code, name, description, billing_model
          FROM entitlements.module WHERE status = 'active' ORDER BY module_code
    """)).fetchall()
    return [dict(r._mapping) for r in rows]


def _q_groups_list(conn: Session, args: dict, actor: str) -> list[dict]:
    """Platform role templates for bank users (read-only: system groups change by migration)."""
    rows = conn.execute(text("""
        SELECT g.id, g.slug, g.label, g.description, g.user_type, g.is_system,
               (SELECT count(*) FROM institution.member m
                 WHERE m.group_id = g.id AND m.deactivated_at IS NULL) AS active_members,
               COALESCE((SELECT jsonb_agg(jsonb_build_object('key', k, 'label', COALESCE(cm.label, k))
                                          ORDER BY k)
                           FROM unnest(g.module_permissions) k
                           LEFT JOIN catalog.module cm ON cm.key = k), '[]'::jsonb) AS modules
          FROM portal_admin.user_groups g
         ORDER BY g.user_type, g.slug
    """)).fetchall()
    return [dict(r._mapping) for r in rows]


def _q_audit_search(conn: Session, args: dict, actor: str) -> list[dict]:
    """The portal's own audit trail, newest first, paged by (occurred_at, id)."""
    iid = _uuid(args["institution_id"], "institution_id") if args.get("institution_id") else None
    prefix = args.get("action_prefix")
    if prefix is not None and (not isinstance(prefix, str) or not prefix
                               or len(prefix) > 64 or not prefix.replace(".", "").replace("_", "").isalnum()):
        raise ChannelError(422, "invalid_field", "action_prefix is not valid")
    before = args.get("before")
    if before is not None:
        try:
            before = datetime.fromisoformat(str(before).replace("Z", "+00:00"))
        except ValueError as e:
            raise ChannelError(422, "invalid_field", "before must be an ISO timestamp") from e
    try:
        limit = max(1, min(int(args.get("limit", 100)), 200))
    except (TypeError, ValueError) as e:
        raise ChannelError(422, "invalid_field", "limit must be a number") from e
    rows = conn.execute(text("""
        SELECT e.id, e.occurred_at, e.actor_type, e.actor_email, e.actor_role, e.actor_ip,
               e.institution_id, i.name AS institution_name, e.action, e.resource_type,
               e.resource_label, e.outcome, e.outcome_note, e.metadata
          FROM audit.event e
          LEFT JOIN institution.institution i ON i.id = e.institution_id
         WHERE (CAST(:iid AS uuid) IS NULL OR e.institution_id = CAST(:iid AS uuid))
           AND (CAST(:prefix AS text) IS NULL OR e.action LIKE :prefix || '%')
           AND (CAST(:before AS timestamptz) IS NULL OR e.occurred_at < CAST(:before AS timestamptz))
         ORDER BY e.occurred_at DESC, e.id DESC
         LIMIT :limit
    """), {"iid": iid, "prefix": prefix, "before": before, "limit": limit}).fetchall()
    return [dict(r._mapping) for r in rows]


def _q_commission_rules(conn: Session, args: dict, actor: str) -> list[dict]:
    rows = conn.execute(text("""
        SELECT r.id AS rule_id, r.institution_id, i.name AS institution_name, r.product_code,
               p.label AS product_name, r.rate, r.basis, r.effective_from, r.ended_on, r.reason,
               r.set_by, r.approved_by, r.created_at,
               (r.effective_from <= (now() AT TIME ZONE 'Indian/Mauritius')::date
                AND (r.ended_on IS NULL
                     OR r.ended_on > (now() AT TIME ZONE 'Indian/Mauritius')::date)) AS in_force
          FROM billing.commission_rule r
          LEFT JOIN institution.institution i ON i.id = r.institution_id
          LEFT JOIN catalog.product p ON p.code = r.product_code
         ORDER BY (r.ended_on IS NULL) DESC, i.name NULLS FIRST, r.product_code NULLS FIRST,
                  r.effective_from DESC
         LIMIT 1000
    """)).fetchall()
    return [dict(r._mapping) for r in rows]


_MONTH = re.compile(r"^(20[2-9][0-9])-(0[1-9]|1[0-2])$")


def _q_commission_statement(conn: Session, args: dict, actor: str) -> dict:
    """One institution's commission for one calendar month (Mauritius time), line by line.

    Read-only: the amounts are exactly what each booking recorded (db/025, db/058), so a
    statement re-run later shows the same lines. A line's month is the month it was booked.
    """
    iid = args.get("institution_id")
    month = str(args.get("month") or "")
    try:
        iid = str(uuid.UUID(str(iid)))
    except (ValueError, TypeError) as e:
        raise ChannelError(422, "invalid_field", "institution_id must be a uuid") from e
    if not _MONTH.match(month):
        raise ChannelError(422, "invalid_field", "month must be YYYY-MM")
    inst = conn.execute(text(
        "SELECT id, name, reg_number, country FROM institution.institution WHERE id = CAST(:i AS uuid)"
    ), {"i": iid}).fetchone()
    if inst is None:
        raise ChannelError(404, "not_found", "institution not found")
    rows = conn.execute(text("""
        SELECT e.id AS commission_id, e.pipeline_id, e.request_id,
               (e.created_at AT TIME ZONE 'Indian/Mauritius')::date AS booked_on,
               p.code AS product_code, p.label AS product_name,
               e.deal_amount, e.commission_rate, e.commission_amt, e.currency,
               e.status, e.invoice_ref, e.invoiced_at, e.paid_at
          FROM portal_admin.commission_event e
          LEFT JOIN marketplace.request r ON r.id = e.request_id
          LEFT JOIN catalog.product p ON p.id = r.product_id
         WHERE e.institution_id = CAST(:i AS uuid)
           AND e.created_at >= (CAST(:m || '-01' AS date)::timestamp AT TIME ZONE 'Indian/Mauritius')
           AND e.created_at <  ((CAST(:m || '-01' AS date) + interval '1 month')::timestamp
                                AT TIME ZONE 'Indian/Mauritius')
         ORDER BY e.created_at, e.id
         LIMIT 5000
    """), {"i": iid, "m": month}).fetchall()
    lines = [dict(r._mapping) for r in rows]
    by_status: dict[str, Decimal] = {}
    total = Decimal("0")
    for ln in lines:
        amt = Decimal(str(ln["commission_amt"] or 0))
        total += amt
        by_status[ln["status"]] = by_status.get(ln["status"], Decimal("0")) + amt
    currencies = sorted({ln["currency"] for ln in lines if ln["currency"]})
    return {
        "institution": dict(inst._mapping), "month": month, "lines": lines,
        "totals": {"deals": len(lines), "commission": total, "by_status": by_status,
                   "currencies": currencies or ["MUR"]},
    }


def _q_products_list(conn: Session, args: dict, actor: str) -> list[dict]:
    rows = conn.execute(text(
        "SELECT code AS product_code, label AS name FROM catalog.product ORDER BY label"
    )).fetchall()
    return [dict(r._mapping) for r in rows]


# Institution applications (db/060). Staff never see unconfirmed ones.
ACCESS_VISIBLE = ("submitted", "in_review", "info_requested", "approved", "rejected", "provisioned")
ACCESS_FILTERS = {
    "open": ("submitted", "in_review", "info_requested", "approved"),
    "closed": ("rejected", "provisioned"),
    **{s: (s,) for s in ACCESS_VISIBLE},
}


def _q_access_requests_list(conn: Session, args: dict, actor: str) -> list[dict]:
    which = args.get("status") or "open"
    if which not in ACCESS_FILTERS:
        raise ChannelError(422, "invalid_field", f"status must be one of {sorted(ACCESS_FILTERS)}")
    rows = conn.execute(text("""
        SELECT id AS request_id, reference, institution_name, institution_type, country,
               reg_number, regulator, website, contact_name, contact_email, contact_phone,
               contact_role, message, status, verified_at, reviewed_by, reviewed_at, review_note,
               provisioned_by, provisioned_at, contract_reference, institution_id,
               created_at, updated_at
          FROM institution.access_request
         WHERE status = ANY(CAST(:st AS text[]))
         ORDER BY CASE status WHEN 'submitted' THEN 0 WHEN 'info_requested' THEN 1
                              WHEN 'in_review' THEN 2 WHEN 'approved' THEN 3 ELSE 4 END,
                  verified_at DESC NULLS LAST
         LIMIT 300
    """), {"st": list(ACCESS_FILTERS[which])}).fetchall()
    return [dict(r._mapping) for r in rows]


SYNC_QUERIES: dict[str, Callable[[Session, dict, str], Any]] = {
    "compliance.overview": _q_compliance_overview,
    "documents.list": _q_documents_list,
    "institutions.list": _q_institutions_list,
    "access_requests.list": _q_access_requests_list,
    "entitlements.catalog": _q_entitlements_catalog,
    "groups.list": _q_groups_list,
    "audit.search": _q_audit_search,
    "commission.rules": _q_commission_rules,
    "commission.statement": _q_commission_statement,
    "products.list": _q_products_list,
}


@router.post("/query", response_model=None)
async def query(request: Request) -> JSONResponse:
    try:
        _, body, claims = await _authenticate(request)
        op = body["op"]
        with service_session() as conn:
            if op == "document.download":
                result = await _q_document_download(conn, body["args"], claims["sub"])
            elif op in SYNC_QUERIES:
                result = SYNC_QUERIES[op](conn, body["args"], claims["sub"])
            else:
                raise ChannelError(404, "unknown_op")
        return JSONResponse(status_code=200, content={"data": _jsonable(result)})
    except ChannelError as e:
        return _err(e.status, e.code, e.detail)


# ── Commands ──────────────────────────────────────────────────────────────────

def _compliance(conn: Session, iid: str) -> dict:
    row = conn.execute(text("""
        SELECT c.institution_id, c.can_bid, c.missing_docs, c.waived_until
          FROM institution.compliance c WHERE c.institution_id = CAST(:iid AS uuid)
    """), {"iid": iid}).fetchone()
    if row is None:
        raise ChannelError(404, "not_found", "Institution not found.")
    d = dict(row._mapping)
    d["missing_docs"] = list(d["missing_docs"] or [])
    return d


def _c_document_review(conn: Session, args: dict, ctx: dict) -> dict:
    doc_id = _uuid(args.get("doc_id"), "doc_id")
    decision = args.get("decision")
    if decision not in ("approve", "reject"):
        raise ChannelError(422, "invalid_field", "decision must be approve or reject")
    reason = _reason(args.get("rejection_reason"), 5) if decision == "reject" else None
    expiry = args.get("expiry_date")
    if expiry is not None:
        try:
            expiry = date.fromisoformat(str(expiry))
        except ValueError as e:
            raise ChannelError(422, "invalid_field", "expiry_date must be YYYY-MM-DD") from e
    row = conn.execute(text("""
        UPDATE institution.doc
           SET status = :status, rejection_reason = :reason,
               expiry_date = COALESCE(CAST(:expiry AS date), expiry_date),
               reviewed_by_staff = CAST(:by AS uuid), reviewed_at = now()
         WHERE id = CAST(:id AS uuid)
        RETURNING id, institution_id, status, rejection_reason, expiry_date, reviewed_at
    """), {"status": "approved" if decision == "approve" else "rejected", "reason": reason,
           "expiry": expiry, "by": ctx["checker"], "id": doc_id}).fetchone()
    if row is None:
        raise ChannelError(404, "not_found", "Document not found.")
    out = dict(row._mapping)
    _audit(conn, ctx["checker"], f"compliance.document_{out['status']}", "institution_doc", doc_id,
           str(out["institution_id"]),
           {"command_id": ctx["command_id"], "rejection_reason": reason})
    out["compliance"] = _compliance(conn, str(out["institution_id"]))
    return out


def _c_waiver_grant(conn: Session, args: dict, ctx: dict) -> dict:
    iid = _uuid(args.get("institution_id"), "institution_id")
    reason = _reason(args.get("reason"))
    try:
        until = datetime.fromisoformat(str(args.get("waived_until")).replace("Z", "+00:00"))
    except ValueError as e:
        raise ChannelError(422, "invalid_field", "waived_until must be an ISO timestamp") from e
    until = until if until.tzinfo else until.replace(tzinfo=UTC)
    now = datetime.now(UTC)
    if until <= now:
        raise ChannelError(422, "invalid_field", "The waiver must end in the future.")
    if until > now + timedelta(days=MAX_WAIVER_DAYS):
        raise ChannelError(422, "invalid_field", f"A waiver can last at most {MAX_WAIVER_DAYS} days.")
    _compliance(conn, iid)
    conn.execute(text("""
        INSERT INTO institution.compliance_waiver
          (institution_id, reason, waived_until, granted_by, granted_at)
        VALUES (CAST(:iid AS uuid), :reason, :until, CAST(:by AS uuid), now())
        ON CONFLICT (institution_id) DO UPDATE SET
          reason = excluded.reason, waived_until = excluded.waived_until,
          granted_by = excluded.granted_by, granted_at = now(),
          revoked_at = NULL, revoked_by = NULL
    """), {"iid": iid, "reason": reason, "until": until, "by": ctx["checker"]})
    _audit(conn, ctx["checker"], "compliance.waiver_granted", "institution", iid, iid,
           {"command_id": ctx["command_id"], "maker_sub": ctx["maker"], "reason": reason,
            "waived_until": until.isoformat()})
    return _compliance(conn, iid)


def _c_waiver_revoke(conn: Session, args: dict, ctx: dict) -> dict:
    iid = _uuid(args.get("institution_id"), "institution_id")
    reason = _reason(args.get("reason"))
    hit = conn.execute(text("""
        UPDATE institution.compliance_waiver
           SET revoked_at = now(), revoked_by = CAST(:by AS uuid)
         WHERE institution_id = CAST(:iid AS uuid) AND revoked_at IS NULL
        RETURNING institution_id
    """), {"iid": iid, "by": ctx["checker"]}).fetchone()
    if hit is None:
        raise ChannelError(404, "not_found", "No active waiver for this institution.")
    _audit(conn, ctx["checker"], "compliance.waiver_revoked", "institution", iid, iid,
           {"command_id": ctx["command_id"], "maker_sub": ctx["maker"], "reason": reason})
    return _compliance(conn, iid)


def _institution_row(conn: Session, iid: str) -> dict:
    row = conn.execute(text("""
        SELECT id AS institution_id, name, approved, onboarding_stage, approved_at,
               suspended_at, suspension_reason
          FROM institution.institution WHERE id = CAST(:iid AS uuid)
    """), {"iid": iid}).fetchone()
    if row is None:
        raise ChannelError(404, "not_found", "Institution not found.")
    return dict(row._mapping)


def _c_institution_approve(conn: Session, args: dict, ctx: dict) -> dict:
    iid = _uuid(args.get("institution_id"), "institution_id")
    before = _institution_row(conn, iid)
    if before["approved"] and before["onboarding_stage"] == "approved":
        raise ChannelError(409, "already_approved", "This bank is already approved.")
    conn.execute(text("""
        UPDATE institution.institution
           SET approved = true, onboarding_stage = 'approved',
               approved_at = COALESCE(approved_at, now()), approved_by = CAST(:by AS uuid),
               suspended_at = NULL, suspended_by = NULL, suspension_reason = NULL,
               updated_at = now()
         WHERE id = CAST(:iid AS uuid)
    """), {"iid": iid, "by": ctx["checker"]})
    _audit(conn, ctx["checker"], "institution.approved", "institution", iid, iid,
           {"command_id": ctx["command_id"], "maker_sub": ctx["maker"], "before": before})
    return _institution_row(conn, iid)


def _c_institution_suspend(conn: Session, args: dict, ctx: dict) -> dict:
    iid = _uuid(args.get("institution_id"), "institution_id")
    reason = _reason(args.get("reason"))
    before = _institution_row(conn, iid)
    if before["onboarding_stage"] == "suspended":
        raise ChannelError(409, "already_suspended", "This bank is already suspended.")
    conn.execute(text("""
        UPDATE institution.institution
           SET approved = false, onboarding_stage = 'suspended', suspended_at = now(),
               suspended_by = CAST(:by AS uuid), suspension_reason = :reason, updated_at = now()
         WHERE id = CAST(:iid AS uuid)
    """), {"iid": iid, "by": ctx["checker"], "reason": reason})
    _audit(conn, ctx["checker"], "institution.suspended", "institution", iid, iid,
           {"command_id": ctx["command_id"], "maker_sub": ctx["maker"], "reason": reason,
            "before": before})
    return _institution_row(conn, iid)


ENTITLEMENT_STATUSES = frozenset({"active", "trial", "suspended", "expired"})
PLAN_TIERS = frozenset({"trial", "standard", "professional", "enterprise"})


def _c_entitlement_set(conn: Session, args: dict, ctx: dict) -> dict:
    iid = _uuid(args.get("institution_id"), "institution_id")
    code = str(args.get("module_code") or "")
    status = args.get("status")
    tier = args.get("plan_tier", "standard")
    if status not in ENTITLEMENT_STATUSES:
        raise ChannelError(422, "invalid_field", "status is not valid")
    if tier not in PLAN_TIERS:
        raise ChannelError(422, "invalid_field", "plan_tier is not valid")
    valid_to = args.get("valid_to")
    if valid_to is not None:
        try:
            valid_to = datetime.fromisoformat(str(valid_to).replace("Z", "+00:00"))
        except ValueError as e:
            raise ChannelError(422, "invalid_field", "valid_to must be an ISO timestamp") from e
        if valid_to.tzinfo is None:
            valid_to = valid_to.replace(tzinfo=UTC)
        if valid_to <= datetime.now(UTC):
            raise ChannelError(422, "invalid_field", "valid_to must be in the future")
    _institution_row(conn, iid)
    if conn.execute(text("SELECT 1 FROM entitlements.module WHERE module_code = :c AND status = 'active'"),
                    {"c": code}).fetchone() is None:
        raise ChannelError(422, "invalid_field", "Unknown or retired module.")
    before = conn.execute(text("""
        SELECT status, plan_tier, valid_to FROM entitlements.institution_entitlement
         WHERE institution_id = CAST(:iid AS uuid) AND module_code = :c
    """), {"iid": iid, "c": code}).fetchone()
    row = conn.execute(text("""
        INSERT INTO entitlements.institution_entitlement
          (institution_id, module_code, plan_tier, status, valid_to, created_by)
        VALUES (CAST(:iid AS uuid), :c, :tier, :status, :valid_to, CAST(:by AS uuid))
        ON CONFLICT (institution_id, module_code) DO UPDATE SET
          plan_tier = excluded.plan_tier, status = excluded.status,
          valid_to = excluded.valid_to, updated_at = now()
        RETURNING institution_id, module_code, plan_tier, status, valid_from, valid_to
    """), {"iid": iid, "c": code, "tier": tier, "status": status, "valid_to": valid_to,
           "by": ctx["checker"]}).fetchone()
    if row is None:  # pragma: no cover - an upsert always returns its row
        raise ChannelError(500, "entitlement_not_saved")
    out = dict(row._mapping)
    _audit(conn, ctx["checker"], "entitlement.set", "institution", iid, iid,
           {"command_id": ctx["command_id"], "maker_sub": ctx["maker"], "module_code": code,
            "before": dict(before._mapping) if before else None, "after": out})
    return out


# ── Commission schedule (db/058) ─────────────────────────────────────────────
# The rules and their invariants live in billing.set_commission_rule() /
# billing.end_commission_rule(); this validates shape first so the common
# mistakes get a clear message, then maps the functions' COMMISSION_* errors.

COMMISSION_MAX_RATE = Decimal("0.05")
_COMMISSION_ERRORS = {
    "COMMISSION_FOUR_EYES": (403, "four_eyes", "The person who asked for this cannot approve it."),
    "COMMISSION_RATE_INVALID": (422, "invalid_field", "rate must be between 0 and 5%."),
    "COMMISSION_DATE_IN_PAST": (422, "invalid_field", "The date must be today or later."),
    "COMMISSION_UNKNOWN_INSTITUTION": (422, "invalid_field", "Unknown institution."),
    "COMMISSION_UNKNOWN_PRODUCT": (422, "invalid_field", "Unknown product."),
    "COMMISSION_LATER_RULE_EXISTS": (409, "later_rule_exists",
                                     "A rule for this scope already starts on or after that date."),
    "COMMISSION_RULE_NOT_OPEN": (404, "not_found", "No open rule with that id."),
    "COMMISSION_DEFAULT_CANNOT_END": (422, "default_cannot_end",
                                      "The platform default can be replaced, not ended."),
}


def _commission_db(conn: Session, sql: str, params: dict) -> dict:
    try:
        row = conn.execute(text(sql), params).fetchone()
    except DBAPIError as e:
        msg = str(getattr(e, "orig", e))
        for key, (status, code, detail) in _COMMISSION_ERRORS.items():
            if key in msg:
                raise ChannelError(status, code, detail) from e
        raise
    if row is None:  # pragma: no cover - the functions always return a row or raise
        raise ChannelError(500, "commission_not_saved")
    return dict(row._mapping)


def _date(v: Any, field: str) -> date:
    try:
        return date.fromisoformat(str(v))
    except ValueError as e:
        raise ChannelError(422, "invalid_field", f"{field} must be YYYY-MM-DD") from e


def _c_commission_set(conn: Session, args: dict, ctx: dict) -> dict:
    iid = args.get("institution_id")
    iid = None if iid in (None, "") else _uuid(iid, "institution_id")
    product = args.get("product_code")
    if product in (None, ""):
        product = None
    elif not isinstance(product, str) or not product.replace("_", "").isalnum() or len(product) > 40:
        raise ChannelError(422, "invalid_field", "product_code is not valid")
    try:
        rate = Decimal(str(args.get("rate")))
    except InvalidOperation as e:
        raise ChannelError(422, "invalid_field", "rate must be a decimal fraction") from e
    if not rate.is_finite() or rate < 0 or rate > COMMISSION_MAX_RATE \
            or rate.as_tuple().exponent < -4:  # type: ignore[operator]
        raise ChannelError(422, "invalid_field",
                           "rate must be a fraction between 0 and 0.05 with at most 4 decimals")
    start = _date(args.get("effective_from"), "effective_from")
    reason = _reason(args.get("reason"))
    out = _commission_db(conn, """
        SELECT * FROM billing.set_commission_rule(
          CAST(:iid AS uuid), :product, CAST(:rate AS numeric), CAST(:start AS date), :reason,
          CAST(:cmd AS uuid), CAST(:maker AS uuid), CAST(:checker AS uuid))
    """, {"iid": iid, "product": product, "rate": str(rate), "start": start.isoformat(),
          "reason": reason, "cmd": ctx["command_id"], "maker": ctx["maker"], "checker": ctx["checker"]})
    _audit(conn, ctx["checker"], "commission.set", "commission_rule", str(out["id"]), iid,
           {"command_id": ctx["command_id"], "maker_sub": ctx["maker"], "product_code": product,
            "rate": str(rate), "effective_from": start.isoformat(), "reason": reason})
    return out


def _c_commission_end(conn: Session, args: dict, ctx: dict) -> dict:
    rid = _uuid(args.get("rule_id"), "rule_id")
    ended_on = _date(args.get("ended_on"), "ended_on")
    reason = _reason(args.get("reason"))
    out = _commission_db(conn, """
        SELECT * FROM billing.end_commission_rule(
          CAST(:rid AS uuid), CAST(:ended AS date), CAST(:cmd AS uuid),
          CAST(:maker AS uuid), CAST(:checker AS uuid))
    """, {"rid": rid, "ended": ended_on.isoformat(), "cmd": ctx["command_id"],
          "maker": ctx["maker"], "checker": ctx["checker"]})
    _audit(conn, ctx["checker"], "commission.ended", "commission_rule", rid,
           str(out["institution_id"]) if out.get("institution_id") else None,
           {"command_id": ctx["command_id"], "maker_sub": ctx["maker"],
            "ended_on": ended_on.isoformat(), "reason": reason})
    return out


# ── Institution applications and provisioning (db/060) ─────

# Who may move an application where. One reviewer decides (the review is the check);
# nothing is created until two admins provision it.
REVIEW_TRANSITIONS: dict[str, frozenset[str]] = {
    "submitted": frozenset({"in_review", "info_requested", "approved", "rejected"}),
    "in_review": frozenset({"info_requested", "approved", "rejected"}),
    "info_requested": frozenset({"in_review", "approved", "rejected"}),
    "approved": frozenset({"in_review", "rejected"}),   # e.g. contract talks fell through
}
# Must equal ficium-auth's UNUSABLE marker (src/api/staff.py): never a valid Argon2
# hash, so the login cannot sign in until the invitee sets a password from the link.
UNUSABLE_PASSWORD = "!invited-not-activated"
DEPLOYMENT_MODELS = frozenset({"saas", "paas", "on_prem"})


def _access_request(conn: Session, rid: str) -> dict:
    row = conn.execute(text("""
        SELECT * FROM institution.access_request WHERE id = CAST(:id AS uuid) FOR UPDATE
    """), {"id": rid}).fetchone()
    if row is None:
        raise ChannelError(404, "not_found", "Application not found.")
    d = dict(row._mapping)
    if d["status"] in ("unverified", "expired"):
        raise ChannelError(404, "not_found", "Application not found.")
    return d


def _c_access_request_review(conn: Session, args: dict, ctx: dict) -> dict:
    rid = _uuid(args.get("request_id"), "request_id")
    to = args.get("status")
    req = _access_request(conn, rid)
    allowed = REVIEW_TRANSITIONS.get(req["status"], frozenset())
    if to not in allowed:
        raise ChannelError(409, "invalid_transition",
                           f"An application that is {req['status']} cannot become {to}.")
    note = _reason(args.get("note"), 10) if to in ("rejected", "info_requested") \
        else (str(args.get("note") or "").strip()[:500] or None)
    conn.execute(text("""
        UPDATE institution.access_request
           SET status = :to, reviewed_by = CAST(:by AS uuid), reviewed_at = now(),
               review_note = :note, updated_at = now()
         WHERE id = CAST(:id AS uuid)
    """), {"to": to, "by": ctx["checker"], "note": note, "id": rid})
    _audit(conn, ctx["checker"], f"access_request.{to}", "access_request", rid, None,
           {"command_id": ctx["command_id"], "from": req["status"], "note": note,
            "reference": req["reference"]})
    return {"request_id": rid, "reference": req["reference"], "status": to, "review_note": note}


def _c_institution_provision(conn: Session, args: dict, ctx: dict) -> dict:
    rid = _uuid(args.get("request_id"), "request_id")
    contract = str(args.get("contract_reference") or "").strip()
    if not 3 <= len(contract) <= 100:
        raise ChannelError(422, "invalid_field", "contract_reference must be 3-100 characters")
    model = args.get("deployment_model") or "saas"
    if model not in DEPLOYMENT_MODELS:
        raise ChannelError(422, "invalid_field", f"deployment_model must be one of {sorted(DEPLOYMENT_MODELS)}")

    req = _access_request(conn, rid)
    if req["status"] != "approved":
        raise ChannelError(409, "not_approved", "Only an approved application can be provisioned.")
    email = req["contact_email"].strip().lower()

    if conn.execute(text("""
        SELECT 1 FROM auth_portal.auth_users WHERE lower(email) = :e OR lower(username) = :e
        UNION ALL SELECT 1 FROM institution.member WHERE lower(email) = :e LIMIT 1
    """), {"e": email}).fetchone():
        raise ChannelError(409, "email_in_use", "That contact email already has a portal account.")
    if conn.execute(text("""
        SELECT 1 FROM institution.institution
         WHERE lower(name) = lower(:n)
            OR (CAST(:reg AS text) IS NOT NULL AND lower(reg_number) = lower(:reg) AND country = :c)
         LIMIT 1
    """), {"n": req["institution_name"], "reg": req["reg_number"], "c": req["country"]}).fetchone():
        raise ChannelError(409, "institution_exists", "An institution with this name or registration already exists.")

    inst = conn.execute(text("""
        INSERT INTO institution.institution
          (name, legal_name, institution_type, country, reg_number, regulator, website,
           deployment_model, modules, onboarding_stage, compliance_status, approved,
           primary_contact_email, primary_contact_name, primary_contact_phone, metadata)
        VALUES
          (:name, :name, :type, :country, :reg, :regulator, :website,
           :model, '["marketplace"]'::jsonb, 'registered', 'not_submitted', false,
           :email, :cname, :phone, CAST(:meta AS jsonb))
        RETURNING id
    """), {"name": req["institution_name"], "type": req["institution_type"],
           "country": req["country"], "reg": req["reg_number"], "regulator": req["regulator"],
           "website": req["website"], "model": model, "email": email,
           "cname": req["contact_name"], "phone": req["contact_phone"],
           "meta": json.dumps({"access_request_id": rid, "access_reference": req["reference"],
                               "contract_reference": contract, "provisioned_by": ctx["checker"],
                               "requested_by": ctx["maker"]})}).fetchone()
    if inst is None:
        raise ChannelError(500, "provision_failed", "The institution could not be created.")
    iid = str(inst.id)
    login_id = str(uuid.uuid4())
    conn.execute(text("""
        INSERT INTO auth_portal.auth_users
            (id, email, username, role, institution_id, password_hash, is_active,
             email_verified, must_change_password, created_at, updated_at)
        VALUES (CAST(:id AS uuid), :email, :email, 'institution_admin', CAST(:iid AS uuid),
                :pw, true, false, false, now(), now())
    """), {"id": login_id, "email": email, "iid": iid, "pw": UNUSABLE_PASSWORD})
    conn.execute(text("""
        INSERT INTO institution.member
          (institution_id, auth_user_id, email, full_name, role, is_primary_admin, active,
           group_id, invited_at)
        VALUES (CAST(:iid AS uuid), CAST(:uid AS uuid), :email, :name, 'admin', true, true,
                (SELECT id FROM portal_admin.user_groups WHERE slug = 'institution_admin' LIMIT 1),
                now())
    """), {"iid": iid, "uid": login_id, "email": email, "name": req["contact_name"]})
    conn.execute(text("""
        UPDATE institution.access_request
           SET status = 'provisioned', provisioned_by = CAST(:by AS uuid), provisioned_at = now(),
               contract_reference = :contract, institution_id = CAST(:iid AS uuid),
               updated_at = now()
         WHERE id = CAST(:id AS uuid)
    """), {"by": ctx["checker"], "contract": contract, "iid": iid, "id": rid})

    meta = {"command_id": ctx["command_id"], "maker_sub": ctx["maker"], "reference": req["reference"],
            "contract_reference": contract, "deployment_model": model, "admin_email": email}
    _audit(conn, ctx["checker"], "institution.provisioned", "institution", iid, iid,
           {**meta, "access_request_id": rid})
    _audit(conn, ctx["checker"], "access_request.provisioned", "access_request", rid, iid, meta)
    return {"institution_id": iid, "institution_name": req["institution_name"],
            "admin_email": email, "admin_login_id": login_id, "reference": req["reference"],
            "deployment_model": model, "onboarding_stage": "registered"}


def _c_institution_invite_admin(conn: Session, args: dict, ctx: dict) -> dict:
    """A first admin for an EXISTING institution that has none (e.g. after a data reset).

    Only when no active primary admin exists, so staff can never add themselves to a
    working bank. Same unusable login + ficium-auth invite as institution.provision.
    """
    iid = _uuid(args.get("institution_id"), "institution_id")
    if iid == settings.platform_institution_id:
        raise ChannelError(422, "invalid_field", "Ficium staff are invited from the Team page.")
    email = str(args.get("email") or "").strip().lower()
    if not (3 <= len(email) <= 254 and re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email)):
        raise ChannelError(422, "invalid_field", "email is not valid")
    name = str(args.get("full_name") or "").strip()
    if not 2 <= len(name) <= 200:
        raise ChannelError(422, "invalid_field", "full_name must be 2-200 characters")
    reason = _reason(args.get("reason"))

    inst = conn.execute(text("""
        SELECT id, name FROM institution.institution WHERE id = CAST(:iid AS uuid) FOR UPDATE
    """), {"iid": iid}).fetchone()
    if inst is None:
        raise ChannelError(404, "not_found", "Institution not found.")
    if conn.execute(text("""
        SELECT 1 FROM institution.member
         WHERE institution_id = CAST(:iid AS uuid) AND is_primary_admin AND active
           AND deactivated_at IS NULL LIMIT 1
    """), {"iid": iid}).fetchone():
        raise ChannelError(409, "has_admin",
                           "This institution already has an active administrator; they invite their own users.")
    if conn.execute(text("""
        SELECT 1 FROM auth_portal.auth_users WHERE lower(email) = :e OR lower(username) = :e
        UNION ALL SELECT 1 FROM institution.member WHERE lower(email) = :e LIMIT 1
    """), {"e": email}).fetchone():
        raise ChannelError(409, "email_in_use", "That email already has a portal account.")

    login_id = str(uuid.uuid4())
    conn.execute(text("""
        INSERT INTO auth_portal.auth_users
            (id, email, username, role, institution_id, password_hash, is_active,
             email_verified, must_change_password, created_at, updated_at)
        VALUES (CAST(:id AS uuid), :email, :email, 'institution_admin', CAST(:iid AS uuid),
                :pw, true, false, false, now(), now())
    """), {"id": login_id, "email": email, "iid": iid, "pw": UNUSABLE_PASSWORD})
    conn.execute(text("""
        INSERT INTO institution.member
          (institution_id, auth_user_id, email, full_name, role, is_primary_admin, active,
           group_id, invited_at)
        VALUES (CAST(:iid AS uuid), CAST(:uid AS uuid), :email, :name, 'admin', true, true,
                (SELECT id FROM portal_admin.user_groups WHERE slug = 'institution_admin' LIMIT 1),
                now())
    """), {"iid": iid, "uid": login_id, "email": email, "name": name})
    _audit(conn, ctx["checker"], "institution.admin_invited", "institution", iid, iid,
           {"command_id": ctx["command_id"], "maker_sub": ctx["maker"], "admin_email": email,
            "reason": reason})
    return {"institution_id": iid, "institution_name": inst.name, "admin_email": email,
            "admin_login_id": login_id}


COMMANDS: dict[str, Callable[[Session, dict, dict], dict]] = {
    "document.review": _c_document_review,
    "waiver.grant": _c_waiver_grant,
    "waiver.revoke": _c_waiver_revoke,
    "institution.approve": _c_institution_approve,
    "institution.suspend": _c_institution_suspend,
    "entitlement.set": _c_entitlement_set,
    "commission.set": _c_commission_set,
    "commission.end": _c_commission_end,
    "access_request.review": _c_access_request_review,
    "institution.provision": _c_institution_provision,
    "institution.invite_admin": _c_institution_invite_admin,
}


def _run_command(raw: bytes, body: dict, actor: str) -> tuple[int, dict]:
    op = body["op"]
    if op not in COMMANDS:
        raise ChannelError(404, "unknown_op")
    command_id = _uuid(body.get("command_id"), "command_id")
    maker = _uuid(body.get("maker_sub"), "maker_sub")
    if op in FOUR_EYES_OPS and maker == actor:
        raise ChannelError(403, "four_eyes", "The person who asked for this cannot approve it.")
    if op not in FOUR_EYES_OPS and maker != actor:
        raise ChannelError(422, "invalid_field", "maker_sub must be the actor for this op")
    body_hash = hashlib.sha256(raw).hexdigest()

    with service_session() as conn:
        # Serialise per command_id so two deliveries of one command cannot both run.
        conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": command_id})
        prev = conn.execute(text(
            "SELECT body_sha256, status_code, response FROM integration.admin_command_log "
            "WHERE command_id = CAST(:k AS uuid)"
        ), {"k": command_id}).fetchone()
        if prev is not None:
            if prev.body_sha256 != body_hash:
                raise ChannelError(409, "command_id_reused", "Same command_id, different body.")
            return int(prev.status_code), dict(prev.response)

        nested = conn.begin_nested()
        try:
            result = COMMANDS[op](conn, body["args"], {
                "command_id": command_id, "maker": maker, "checker": actor})
            nested.commit()
            status, payload = 200, {"data": _jsonable(result)}
        except ChannelError as e:
            nested.rollback()
            if e.status >= 500:
                raise
            # A refused command is recorded too, so a retry gets the same answer.
            status, payload = e.status, {"error": e.code, **({"detail": e.detail} if e.detail else {})}

        conn.execute(text("""
            INSERT INTO integration.admin_command_log
              (command_id, op, body_sha256, maker_sub, checker_sub, status_code, response)
            VALUES (CAST(:k AS uuid), :op, :h, CAST(:m AS uuid), CAST(:c AS uuid), :s, CAST(:r AS jsonb))
        """), {"k": command_id, "op": op, "h": body_hash, "m": maker, "c": actor,
               "s": status, "r": json.dumps(payload)})
        return status, payload


@router.post("/commands", response_model=None)
async def commands(request: Request) -> JSONResponse:
    try:
        raw, body, claims = await _authenticate(request)
        status, payload = await asyncio.to_thread(_run_command, raw, body, claims["sub"])
        return JSONResponse(status_code=status, content=payload)
    except ChannelError as e:
        return _err(e.status, e.code, e.detail)
