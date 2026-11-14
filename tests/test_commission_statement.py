"""commission.statement: one institution, one month, exactly what each booking recorded."""
from __future__ import annotations

import os
import uuid
from decimal import Decimal
from typing import Any

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("APP_DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("APP_SERVICE_SECRET", "test-secret-1234")

from app.api import internal_admin as ia  # noqa: E402

IID = str(uuid.uuid4())


class _Row:
    def __init__(self, **kw: Any) -> None:
        self._mapping = kw


class _R:
    def __init__(self, one: Any = None, rows: list[Any] | None = None) -> None:
        self._one, self._rows = one, rows or []

    def fetchone(self) -> Any:
        return self._one

    def fetchall(self) -> list[Any]:
        return self._rows


class Conn:
    def __init__(self, inst: bool = True, lines: list[dict] | None = None) -> None:
        self.inst, self.lines = inst, lines or []
        self.params: list[dict] = []

    def execute(self, stmt: Any, params: dict | None = None) -> _R:
        self.params.append(params or {})
        q = " ".join(str(stmt).split())
        if "FROM institution.institution" in q:
            row = _Row(id=IID, name="MCB", reg_number="C1", country="MU")
            return _R(row if self.inst else None)
        return _R(rows=[_Row(**ln) for ln in self.lines])


LINE = dict(commission_id="c", pipeline_id="p", request_id="r", booked_on="2026-11-03",
            product_code="personal_loan", product_name="Personal loan",
            deal_amount=Decimal("500000"),
            commission_rate=Decimal("0.0090"), commission_amt=Decimal("4500.00"), currency="MUR",
            status="pending", invoice_ref=None, invoiced_at=None, paid_at=None)


def test_totals_by_status_from_recorded_amounts() -> None:
    c = Conn(lines=[LINE, {**LINE, "commission_amt": Decimal("1000.50"), "status": "paid"}])
    out = ia._q_commission_statement(c, {"institution_id": IID, "month": "2026-11"}, "staff")
    assert out["institution"]["name"] == "MCB"
    assert out["totals"]["deals"] == 2
    assert out["totals"]["commission"] == Decimal("5500.50")
    assert out["totals"]["by_status"] == {"pending": Decimal("4500.00"), "paid": Decimal("1000.50")}
    assert c.params[-1] == {"i": IID, "m": "2026-11"}


def test_empty_month_is_a_valid_statement() -> None:
    out = ia._q_commission_statement(Conn(), {"institution_id": IID, "month": "2026-01"}, "staff")
    assert out["lines"] == [] and out["totals"]["commission"] == 0
    assert out["totals"]["currencies"] == ["MUR"]


@pytest.mark.parametrize("args", [
    {"institution_id": "nope", "month": "2026-11"},
    {"institution_id": IID, "month": "2026-13"},
    {"institution_id": IID, "month": "2026-11'; drop"},
    {"institution_id": IID},
])
def test_bad_arguments_are_refused_before_any_query(args: dict) -> None:
    c = Conn()
    with pytest.raises(ia.ChannelError) as e:
        ia._q_commission_statement(c, args, "staff")
    assert e.value.status == 422 and c.params == []


def test_unknown_institution_is_404() -> None:
    with pytest.raises(ia.ChannelError) as e:
        args = {"institution_id": IID, "month": "2026-11"}
        ia._q_commission_statement(Conn(inst=False), args, "staff")
    assert e.value.status == 404


def test_statement_is_registered_on_the_channel() -> None:
    assert ia.SYNC_QUERIES["commission.statement"] is ia._q_commission_statement
