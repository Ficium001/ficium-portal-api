"""Structured benefit values (db/028): validation and wording."""
from __future__ import annotations

import os
from decimal import Decimal
from typing import Any

import pytest
from fastapi import HTTPException

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("APP_DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("APP_SERVICE_SECRET", "test-secret-1234")

from app.api.benefits import _display, _normalise  # noqa: E402


class _Conn:
    def __init__(self, code: str | None) -> None:
        self.code = code

    def execute(self, stmt: Any, params: Any = None) -> Any:
        code = self.code

        class R:
            def scalar(self_inner) -> str | None:
                return code
        return R()


def norm(code: str, **body: Any) -> dict:
    return _normalise(_Conn(code), {"cat_id": "c1", "title": "Launch offer", **body})  # type: ignore[arg-type]


def test_rate_discount_needs_rate_points() -> None:
    rec = norm("rate_discount", value_amount="0.25", value_unit="pct_pa")
    assert rec["value_amount"] == Decimal("0.25")
    assert rec["value_display"] == "0.25% p.a. off the rate"


@pytest.mark.parametrize(("body", "msg"), [
    ({}, "needs a value"),
    ({"value_amount": "0.25", "value_unit": "mur"}, "Unit must be"),
    ({"value_amount": "7", "value_unit": "pct_pa"}, "at most 5"),
    ({"value_amount": "0", "value_unit": "pct_pa"}, "more than 0"),
    ({"value_amount": "abc", "value_unit": "pct_pa"}, "number"),
])
def test_rate_discount_rejects(body: dict, msg: str) -> None:
    with pytest.raises(HTTPException) as e:
        norm("rate_discount", **body)
    assert e.value.status_code == 422 and msg in e.value.detail


def test_fee_waiver_amount_or_percent() -> None:
    mur = norm("fee_waiver", value_amount="5000", value_unit="mur")
    pct = norm("fee_waiver", value_amount="50", value_unit="pct")
    assert mur["value_display"] == "MUR 5,000 fee waived"
    assert pct["value_display"] == "50% of the fee waived"


def test_qualitative_category_drops_any_value() -> None:
    rec = norm("relationship_mgr", value_amount="10", value_unit="mur")
    assert rec["value_amount"] is None and rec["value_unit"] is None
    assert rec["value_display"] is None


def test_insurance_value_optional() -> None:
    assert norm("insurance")["value_display"] is None
    rec = norm("insurance", value_amount="1000000", value_unit="mur")
    assert rec["value_display"] == "Cover up to MUR 1,000,000"


@pytest.mark.parametrize("title", ["", "x", "y" * 81])
def test_title_length(title: str) -> None:
    with pytest.raises(HTTPException):
        norm("other", title=title)


def test_dates_in_order() -> None:
    with pytest.raises(HTTPException):
        norm("other", valid_from="2026-11-01", valid_until="2026-10-01")


def test_unknown_category() -> None:
    with pytest.raises(HTTPException):
        _normalise(_Conn(None), {"cat_id": "x", "title": "abc"})  # type: ignore[arg-type]


def test_update_merges_over_existing_record() -> None:
    existing = {"cat_id": "c1", "title": "Old", "description": "kept",
                "value_amount": Decimal("0.5"), "value_unit": "pct_pa"}
    rec = _normalise(_Conn("rate_discount"), {"title": "New title"}, existing)  # type: ignore[arg-type]
    assert rec["description"] == "kept" and rec["value_amount"] == Decimal("0.5")


def test_display_none_without_value() -> None:
    assert _display("reward", None, None) is None
