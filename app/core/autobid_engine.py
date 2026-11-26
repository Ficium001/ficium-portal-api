"""The autobid engine: for a new biddable request, every bank that switched
autobid on decides, in its own way, whether to bid.

Per bank (autobid.institution_config, changed only with four eyes):
  decision_mode   rules | external | rules_then_external
  approval_mode   queue_for_approval | pre_approved
plus bank-wide daily caps and an optional product list.

Order for each bank, each step logged to autobid.execution so every outcome
(including "no rule matched") can be explained later:
  1. claim (bank, request) once, ever: two replicas or a retry never bid twice
  2. compliance pre-check (the bid guard in the database enforces it again)
  3. rules: the winning approved rule, and its own daily guardrails
  4. the bank's decision engine, when the mode asks it
  5. bank-wide daily caps, under a row lock so parallel requests cannot jointly
     breach them
  6. pre_approved -> the bid is placed (app.core.bidding.place_bid, the one bid
     path, submitted_via 'autobid'); queue_for_approval -> the bid waits in the
     bank's bid approval, maker = the person who submitted the rule (or set up
     the engine), so a different person must approve it

Triggered when a request first arrives (integration events) and by a sweep in
the maintenance loop that catches anything missed. Off globally with
AUTOBID_ENGINE_ENABLED=false; per bank with the kill switch in the portal.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from . import autobid_decision
from .autobid_facts import load_request, request_object
from .autobid_rules import CandidateRule, PricedBid, price, select_winning_rule
from .bidding import BidInput, BidPlacementError, place_bid
from .config import settings
from .db import service_session
from .entitlements import record_usage

log = structlog.get_logger(__name__)

MODULE = "AUTOBID"

# Banks that may auto-bid on this request: autobid on, licensed, the product
# enabled for the bank (and in its autobid product list, if it set one), no bid yet.
_ELIGIBLE_SQL = """
    SELECT c.institution_id, c.decision_mode, c.approval_mode, c.engine_url,
           c.engine_signing_secret, c.engine_timeout_ms, c.engine_failure_mode,
           c.max_bids_per_day, c.max_amount_per_day, c.changed_by, c.version
      FROM autobid.institution_config c
      JOIN entitlements.institution_entitlement e
        ON e.institution_id = c.institution_id AND e.module_code = 'AUTOBID'
       AND e.status IN ('active', 'trial') AND e.valid_from <= now()
       AND (e.valid_to IS NULL OR e.valid_to > now())
      JOIN institution.product_config pc
        ON pc.institution_id = c.institution_id AND pc.product_id = CAST(:pid AS uuid)
       AND pc.enabled = true
     WHERE c.enabled = true
       AND (c.product_codes IS NULL OR :ptype = ANY (c.product_codes))
       AND NOT EXISTS (SELECT 1 FROM marketplace.bid b
                        WHERE b.institution_id = c.institution_id
                          AND b.request_id = CAST(:rid AS uuid))
       AND NOT EXISTS (SELECT 1 FROM autobid.claim k
                        WHERE k.institution_id = c.institution_id
                          AND k.request_id = CAST(:rid AS uuid))
"""

_RULES_SQL = """
    SELECT r.id AS rule_id, r.name, r.priority, v.id AS rule_version_id, v.version_no,
           v.predicate, v.action, v.guardrails, v.approved_at, v.submitted_by
      FROM autobid.rule r
      JOIN autobid.rule_version v ON v.id = r.current_version_id
     WHERE r.institution_id = CAST(:iid AS uuid) AND r.status = 'active'
       AND v.approved_at IS NOT NULL
"""


@dataclass
class Outcome:
    institution_id: str
    outcome: str
    rule_id: str | None = None
    rule_version_id: str | None = None
    bid_id: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    logged: bool = False  # _act() writes its own log row inside the bid transaction


# ---------------------------------------------------------------------------
# Pure decision helpers (unit-tested without a database)
# ---------------------------------------------------------------------------


def active_rules(rows: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    """Approved rules whose guardrails have not expired."""
    out = []
    for r in rows:
        exp = (r.get("guardrails") or {}).get("expires_at")
        if exp:
            try:
                at = datetime.fromisoformat(str(exp))
            except ValueError:
                continue
            if at.tzinfo is None:
                at = at.replace(tzinfo=UTC)
            if at <= now:
                continue
        out.append(r)
    return out


def pick_rule(rows: list[dict[str, Any]], facts: dict[str, Any]) -> dict[str, Any] | None:
    by_version = {str(r["rule_version_id"]): r for r in rows}
    cands = [
        CandidateRule(
            str(r["rule_id"]),
            str(r["rule_version_id"]),
            int(r["priority"]),
            r["approved_at"],
            r["predicate"],
        )
        for r in rows
    ]
    win = select_winning_rule(cands, facts)
    return by_version[win.rule_version_id] if win else None


def caps_allow(
    bids_today: int, amount_today: float, amount: float, max_bids: int, max_amount: float
) -> str | None:
    """None when another bid of `amount` fits the daily caps, else why not."""
    if bids_today + 1 > max_bids:
        return f"daily bid limit reached ({max_bids})"
    if amount_today + amount > max_amount:
        return f"daily amount limit reached ({max_amount:,.0f})"
    return None


# ---------------------------------------------------------------------------
# Database steps
# ---------------------------------------------------------------------------


def _claim(institution_id: str, request_id: str) -> bool:
    with service_session() as conn:
        row = conn.execute(
            text("""
            INSERT INTO autobid.claim (institution_id, request_id)
            VALUES (CAST(:iid AS uuid), CAST(:rid AS uuid))
            ON CONFLICT DO NOTHING RETURNING 1
        """),
            {"iid": institution_id, "rid": request_id},
        ).first()
    return row is not None


def _can_bid(conn: Session, institution_id: str) -> tuple[bool, list[str]]:
    row = (
        conn.execute(
            text("""
        SELECT can_bid, missing_docs FROM institution.compliance
         WHERE institution_id = CAST(:iid AS uuid)
    """),
            {"iid": institution_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        return False, []
    return bool(row["can_bid"]), list(row["missing_docs"] or [])


def _rule_usage_today(conn: Session, institution_id: str, rule_id: str) -> tuple[int, float]:
    row = (
        conn.execute(
            text("""
        SELECT count(*) AS n, coalesce(sum((detail ->> 'amount')::numeric), 0) AS amt
          FROM autobid.execution
         WHERE institution_id = CAST(:iid AS uuid) AND rule_id = CAST(:rule AS uuid)
           AND outcome IN ('bid_placed', 'queued_for_approval')
           AND occurred_at >= date_trunc('day', now())
    """),
            {"iid": institution_id, "rule": rule_id},
        )
        .mappings()
        .first()
    )
    return (int(row["n"]), float(row["amt"])) if row else (0, 0.0)


def _lock_exposure(conn: Session, institution_id: str) -> tuple[int, float]:
    conn.execute(
        text("""
        INSERT INTO autobid.exposure (institution_id) VALUES (CAST(:iid AS uuid))
        ON CONFLICT (institution_id) DO NOTHING
    """),
        {"iid": institution_id},
    )
    row = (
        conn.execute(
            text("""
        SELECT bids_today, principal_today_mur, today = current_date AS same_day
          FROM autobid.exposure WHERE institution_id = CAST(:iid AS uuid) FOR UPDATE
    """),
            {"iid": institution_id},
        )
        .mappings()
        .first()
    )
    assert row is not None
    if not row["same_day"]:
        return 0, 0.0
    return int(row["bids_today"]), float(row["principal_today_mur"])


def _bump_exposure(conn: Session, institution_id: str, bids: int, amount: float) -> None:
    conn.execute(
        text("""
        UPDATE autobid.exposure
           SET bids_today = :b, principal_today_mur = :a, today = current_date, updated_at = now()
         WHERE institution_id = CAST(:iid AS uuid)
    """),
        {"iid": institution_id, "b": bids, "a": amount},
    )


def _log(conn: Session, request_id: str, o: Outcome) -> None:
    o.logged = True
    conn.execute(
        text("""
        INSERT INTO autobid.execution
            (institution_id, rule_id, rule_version_id, loan_request_id, outcome, bid_id,
             detail, idempotency_key)
        VALUES (CAST(:iid AS uuid), CAST(:rule AS uuid), CAST(:ver AS uuid), CAST(:rid AS uuid),
                :outcome, CAST(:bid AS uuid), CAST(:detail AS jsonb), :key)
    """),
        {
            "iid": o.institution_id,
            "rule": o.rule_id,
            "ver": o.rule_version_id,
            "rid": request_id,
            "outcome": o.outcome,
            "bid": o.bid_id,
            "detail": json.dumps(o.detail, default=str),
            "key": f"{o.institution_id}:{request_id}",
        },
    )


def _log_alone(request_id: str, o: Outcome) -> None:
    with service_session() as conn:
        _log(conn, request_id, o)


# ---------------------------------------------------------------------------
# One bank, one request
# ---------------------------------------------------------------------------


def _ids(rule: dict[str, Any] | None) -> dict[str, Any]:
    if rule is None:
        return {}
    return {"rule_id": str(rule["rule_id"]), "rule_version_id": str(rule["rule_version_id"])}


def decide_for_bank(cfg: dict[str, Any], req: dict[str, Any], now: datetime) -> Outcome:
    iid = str(cfg["institution_id"])
    rid = str(req["id"])
    obj = request_object(req)
    facts = obj["facts"]
    base = {
        "decision_mode": cfg["decision_mode"],
        "approval_mode": cfg["approval_mode"],
        "config_version": cfg["version"],
        "product": facts.get("product"),
    }

    with service_session() as conn:
        ok, missing = _can_bid(conn, iid)
        if not ok:
            return Outcome(iid, "skipped_compliance", detail={**base, "missing_docs": missing})
        rules = active_rules(
            [dict(r) for r in conn.execute(text(_RULES_SQL), {"iid": iid}).mappings().all()], now
        )

    rule: dict[str, Any] | None = None
    priced: PricedBid | None = None
    if cfg["decision_mode"] in ("rules", "rules_then_external"):
        rule = pick_rule(rules, facts)
        if rule is None:
            return Outcome(iid, "no_match", detail={**base, "rules_checked": len(rules)})
        ids: dict[str, Any] = {
            "rule_id": str(rule["rule_id"]),
            "rule_version_id": str(rule["rule_version_id"]),
        }
        g = rule["guardrails"]
        priced = price(rule["action"], facts)
        if priced is None:
            return Outcome(
                iid, "error", **ids, detail={**base, "reason": "rule cannot price this request"}
            )
        with service_session() as conn:
            n, amt = _rule_usage_today(conn, iid, ids["rule_id"])
        why = caps_allow(
            n, amt, priced.amount, int(g["max_bids_per_day"]), float(g["max_amount_per_day"])
        )
        if why:
            return Outcome(
                iid, "suppressed_guardrail", **ids, detail={**base, "reason": f"rule {why}"}
            )

    engine: dict[str, Any] = {}
    if cfg["decision_mode"] in ("external", "rules_then_external"):
        rule_info = None
        if rule is not None and priced is not None:
            rule_info = {
                "id": str(rule["rule_id"]),
                "version": rule["version_no"],
                "name": rule["name"],
                "suggested_bid": priced.__dict__,
            }
        answer = autobid_decision.ask(
            cfg["engine_url"],
            cfg["engine_signing_secret"] or "",
            int(cfg["engine_timeout_ms"]),
            iid,
            obj,
            rule_info,
            decision_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"autobid:{iid}:{rid}")),
        )
        engine = {
            "reference": answer.reference,
            "latency_ms": answer.latency_ms,
            "status_code": answer.status_code,
            "reason": answer.reason,
        }
        ids = _ids(rule)
        if answer.decision == "decline":
            return Outcome(iid, "declined_by_engine", **ids, detail={**base, "engine": engine})
        if answer.decision == "unavailable":
            if not (cfg["engine_failure_mode"] == "use_rule" and priced is not None):
                return Outcome(iid, "engine_unavailable", **ids, detail={**base, "engine": engine})
            engine["fallback"] = "rule price used"
        else:
            priced = PricedBid(
                float(answer.rate_pct or 0),
                answer.rate_type or "fixed",
                float(answer.amount or 0),
                int(answer.term_months or 0),
                int(answer.rate_valid_days or 14),
                answer.conditions,
            )

    assert priced is not None
    ids = _ids(rule)
    bid_detail = {
        **base,
        "rate_pct": priced.rate_pct,
        "rate_type": priced.rate_type,
        "amount": priced.amount,
        "term_months": priced.term_months,
        "rate_valid_days": priced.rate_valid_days,
    }
    if engine:
        bid_detail["engine"] = engine
    return _act(cfg, rid, priced, rule, ids, bid_detail)


def _act(
    cfg: dict[str, Any],
    rid: str,
    priced: PricedBid,
    rule: dict[str, Any] | None,
    ids: dict[str, Any],
    detail: dict[str, Any],
) -> Outcome:
    iid = str(cfg["institution_id"])
    with service_session() as conn:
        bids_today, amount_today = _lock_exposure(conn, iid)
        why = caps_allow(
            bids_today,
            amount_today,
            priced.amount,
            int(cfg["max_bids_per_day"]),
            float(cfg["max_amount_per_day"]),
        )
        if why:
            o = Outcome(
                iid, "suppressed_exposure", **ids, detail={**detail, "reason": f"bank {why}"}
            )
            _log(conn, rid, o)
            return o

        conditions: dict[str, Any] = {"autobid": True}
        if priced.conditions:
            conditions["text"] = priced.conditions
        if cfg["approval_mode"] == "pre_approved":
            try:
                result = place_bid(
                    conn,
                    BidInput(
                        request_id=rid,
                        institution_id=iid,
                        rate=priced.rate_pct,
                        rate_type=priced.rate_type,
                        amount_offered=priced.amount,
                        term_months=priced.term_months,
                        rate_valid_days=priced.rate_valid_days,
                        conditions=conditions,
                        idempotency_key=f"autobid:{iid}:{rid}",
                        submitted_via="autobid",
                    ),
                )
            except BidPlacementError as e:
                conn.rollback()
                o = Outcome(iid, "error", **ids, detail={**detail, "reason": e.detail})
                _log(conn, rid, o)
                return o
            except DBAPIError as e:
                conn.rollback()
                msg = str(getattr(e, "orig", e)).split("\n")[0][:300]
                kind = "skipped_compliance" if "cannot bid yet" in msg else "error"
                o = Outcome(iid, kind, **ids, detail={**detail, "reason": msg})
                _log(conn, rid, o)
                return o
            o = Outcome(iid, "bid_placed", **ids, bid_id=result.id, detail=detail)
        else:
            maker = (rule or {}).get("submitted_by") or cfg.get("changed_by")
            if not maker:
                o = Outcome(
                    iid,
                    "error",
                    **ids,
                    detail={**detail, "reason": "no maker to queue the bid under"},
                )
                _log(conn, rid, o)
                return o
            payload = {
                "request_id": rid,
                "rate": priced.rate_pct,
                "rate_type": priced.rate_type,
                "amount_offered": priced.amount,
                "term_months": priced.term_months,
                "rate_valid_days": priced.rate_valid_days,
                "conditions": conditions,
            }
            label = f"Auto-bid: {rule['name']}" if rule else "Auto-bid: decision engine"
            try:
                action_id = conn.execute(
                    text("""
                    SELECT autobid.queue_bid(CAST(:iid AS uuid), CAST(:maker AS uuid),
                                             CAST(:rid AS uuid), CAST(:p AS jsonb), :label)
                """),
                    {
                        "iid": iid,
                        "maker": str(maker),
                        "rid": rid,
                        "p": json.dumps(payload),
                        "label": label,
                    },
                ).scalar()
            except DBAPIError as e:
                conn.rollback()
                msg = str(getattr(e, "orig", e)).split("\n")[0][:300]
                o = Outcome(iid, "error", **ids, detail={**detail, "reason": msg})
                _log(conn, rid, o)
                return o
            o = Outcome(
                iid, "queued_for_approval", **ids, detail={**detail, "action_id": str(action_id)}
            )

        _bump_exposure(conn, iid, bids_today + 1, amount_today + priced.amount)
        record_usage(
            conn,
            institution_id=iid,
            module_code=MODULE,
            unit="executed_bid",
            idempotency_key=f"autobid:{iid}:{rid}",
            metadata={"outcome": o.outcome, "rule_version_id": ids.get("rule_version_id")},
        )
        _log(conn, rid, o)
        return o


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def evaluate_request(request_id: str, now: datetime | None = None) -> list[Outcome]:
    """Run every eligible bank for one request. Safe to call any number of times."""
    if not settings.autobid_engine_enabled:
        return []
    now = now or datetime.now(UTC)
    with service_session() as conn:
        req = load_request(conn, request_id)
        if req is None or req["status"] != "bidding":
            return []
        if req["bid_window_closes_at"] and req["bid_window_closes_at"] <= now:
            return []
        banks = [
            dict(r)
            for r in conn.execute(
                text(_ELIGIBLE_SQL),
                {
                    "rid": request_id,
                    "pid": str(req["product_id"]),
                    "ptype": req["product_type"],
                },
            )
            .mappings()
            .all()
        ]

    outcomes = []
    for cfg in banks:
        iid = str(cfg["institution_id"])
        if not _claim(iid, request_id):
            continue
        try:
            o = decide_for_bank(cfg, req, now)
        except Exception as e:  # noqa: BLE001 - one bank's failure never stops the others
            log.exception("autobid_bank_failed", institution_id=iid, request_id=request_id)
            o = Outcome(iid, "error", detail={"reason": type(e).__name__})
        if not o.logged:
            _maybe_log(request_id, o)
        log.info("autobid_outcome", institution_id=iid, request_id=request_id, outcome=o.outcome)
        outcomes.append(o)
    return outcomes


def _maybe_log(request_id: str, o: Outcome) -> None:
    try:
        _log_alone(request_id, o)
    except Exception:  # noqa: BLE001
        log.exception("autobid_log_failed", institution_id=o.institution_id, request_id=request_id)


def schedule(request_id: str) -> None:
    """Fire-and-forget from an async endpoint, after the request is committed."""
    if not settings.autobid_engine_enabled:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(asyncio.to_thread(evaluate_request, request_id))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


_TASKS: set[asyncio.Task[Any]] = set()

_SWEEP_SQL = """
    SELECT r.id FROM marketplace.request r
     WHERE r.status = 'bidding' AND r.bid_window_closes_at > now()
       AND r.created_at > now() - interval '7 days'
       AND EXISTS (
         SELECT 1 FROM autobid.institution_config c
          WHERE c.enabled
            AND NOT EXISTS (SELECT 1 FROM autobid.claim k
                             WHERE k.institution_id = c.institution_id AND k.request_id = r.id))
     ORDER BY r.created_at
     LIMIT 200
"""


def sweep() -> int:
    """Catch requests the event trigger missed (restart, deploy, a bank switched on
    later). Returns how many requests were looked at."""
    if not settings.autobid_engine_enabled:
        return 0
    with service_session() as conn:
        ids = [str(r[0]) for r in conn.execute(text(_SWEEP_SQL)).fetchall()]
    for rid in ids:
        try:
            evaluate_request(rid)
        except Exception:  # noqa: BLE001
            log.exception("autobid_sweep_request_failed", request_id=rid)
    return len(ids)
