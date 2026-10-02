"""The employer name must never reach institutions (decision 2026-10-02)."""
from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("APP_DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("APP_SERVICE_SECRET", "test-secret-1234")

from app.api import marketplace  # noqa: E402


def _row(**over) -> SimpleNamespace:
    base = dict(
        product_type="personal_loan", amount=250000, preferred_term_months=36,
        monthly_income=85000, snap_income=None, snap_net_worth=900000, total_net_worth=None,
        monthly_loan_payments=12000, mortgage_balance=0, personal_loan_balance=100000,
        credit_card_balance=5000, vehicle_loan_balance=0, kyc_status="verified",
        employment_status="employed", employment_type="permanent", years_of_employment=6,
        has_existing_loans=True, loan_breakdown=None, health_score=70, risk_score=25,
        affordability_score=80, client_age=34, product_answers={},
        # a row that still carried the old columns must not leak them either
        emp_employer_name="Example Ltd", dossier_employer="Example Ltd",
    )
    return SimpleNamespace(**{**base, **over})


def test_phase1_never_contains_the_employer() -> None:
    p1 = marketplace._build_phase1(_row(), {"purpose": "Home improvement"})
    assert "employer" not in p1
    assert "Example Ltd" not in str(p1)
    assert p1["employment_status"] == "employed" and p1["years_employed"] == 6.0


def test_enrichment_query_no_longer_selects_the_employer() -> None:
    assert "employer" not in marketplace._ENRICH_SQL.lower()
