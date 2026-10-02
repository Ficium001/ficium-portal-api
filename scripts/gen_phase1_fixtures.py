"""Generate the Phase 1 golden fixtures from the REAL Python builder (marketplace._build_phase1).

The same file is then run against the SQL port (integration.phase1_from_row on the borrower DB), so the two
implementations are proven equal on every case. Synthetic data only. Usage:
    python scripts/gen_phase1_fixtures.py <output-dir>
"""
from __future__ import annotations

import json
import os
import random
import re
import sys
from decimal import Decimal
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "postgresql://t:t@localhost:5432/t")
os.environ.setdefault("APP_DATABASE_URL", "postgresql://t:t@localhost:5432/t")
os.environ.setdefault("APP_SERVICE_SECRET", "x")
from app.api import marketplace as m  # noqa: E402

FIELDS = ["product_type", "amount", "preferred_term_months", "purpose", "product_answers", "kyc_status", "client_age",
          "employment_status", "monthly_income", "total_net_worth", "has_existing_loans", "health_score", "risk_score",
          "affordability_score", "employment_type", "years_of_employment", "monthly_loan_payments", "snap_income",
          "snap_net_worth", "mortgage_balance", "personal_loan_balance", "credit_card_balance", "vehicle_loan_balance",
          "loan_breakdown"]
MONEY = {"amount", "monthly_income", "total_net_worth", "years_of_employment", "monthly_loan_payments", "snap_income",
         "snap_net_worth", "mortgage_balance", "personal_loan_balance", "credit_card_balance", "vehicle_loan_balance"}
TYPES = ["sme_loan", "personal_loan", "mortgage", "fixed_deposit", "savings_account", "credit_card", "business_account",
         "investment_account", "leasing", "overdraft", "business_loan", "equities", "unit_trust", "savings_plan",
         "government_bonds", "offshore_investment", "mixed_portfolio"]


def _floatify(v):
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, list):
        return [_floatify(x) for x in v]
    if isinstance(v, dict):
        return {k: _floatify(x) for k, x in v.items()}
    return v


def run_builder(row_json: str) -> dict:
    # Like production: money COLUMNS arrive as Decimal; JSON columns (product_answers, loan_breakdown) as plain floats.
    d = json.loads(row_json, parse_float=Decimal)
    d = {k: (v if k in MONEY else _floatify(v)) for k, v in d.items()}
    row = SimpleNamespace(**{k: d.get(k) for k in FIELDS})
    return m._build_phase1(row, m._parse_purpose(row.purpose))


def emit(row: dict) -> str:
    """JSON with money as raw numbers (Decimal-safe), everything else normal."""
    def enc(k, v):
        return f"__D__{v}" if k in MONEY and isinstance(v, str) else v
    text = json.dumps({k: enc(k, v) for k, v in row.items()})
    return re.sub(r'"__D__([^"]+)"', r"\1", text)


def base(**o) -> dict:
    r = {k: None for k in FIELDS}
    r.update(product_type="personal_loan", amount="250000.00", preferred_term_months=36, product_answers={})
    r.update(o)
    return r


HAND = {
 "empty_row": base(),
 "personal_full": base(purpose="Purpose: Home improvement | Monthly Debt: 5000", kyc_status="verified", client_age=34,
     employment_status="employed", employment_type="permanent", years_of_employment="6.5", monthly_income="85000",
     snap_net_worth="900000", monthly_loan_payments="12000", personal_loan_balance="100000", credit_card_balance="5000",
     has_existing_loans=True, health_score=70, risk_score=25, affordability_score=80,
     loan_breakdown=[{"type": "personal", "outstanding": 100000, "monthly": 8000, "bank": None, "months_left": 24}]),
 "mortgage_residential": base(product_type="mortgage", amount="3000000", preferred_term_months=240,
     purpose="Purpose: Buy a house | Property Type: Apartment | Property Value: 4,000,000", monthly_income="150000"),
 "mortgage_clean_value": base(product_type="mortgage", amount="3000000", preferred_term_months=240,
     purpose="purpose: Buy | property type: Residential land | property value: 4000000", monthly_income="150000", monthly_loan_payments="20000"),
 "mortgage_zero_value": base(product_type="mortgage", purpose="Property Value: 0 | Vehicle Value: 900000", monthly_income="100000"),
 "mortgage_junk_value": base(product_type="mortgage", purpose="Property Value: abc", monthly_income="100000"),
 "business_loan": base(product_type="business_loan", amount="1500000", preferred_term_months=60, monthly_income="400000"),
 "vehicle_alias_make": base(product_type="vehicle", purpose="Vehicle Make: Toyota | Vehicle Type: SUV | Vehicle Value: 1200000", monthly_income="90000"),
 "vehicle_alias_type_only": base(product_type="car_loan", purpose="Vehicle Make:  | Vehicle Type: Sedan | Vehicle Value: 800000", monthly_income="90000"),
 "dup_keys_last_wins": base(purpose="Purpose: first | purpose: second | | no colon here | :empty key | Key With Space: v"),
 "pipes_and_spaces": base(purpose="  Purpose :  spaced out  |Monthly Debt:1e3|  | x: y: z "),
 "zero_term": base(preferred_term_months=0, monthly_income="50000", monthly_loan_payments="1000"),
 "null_term": base(preferred_term_months=None, monthly_income="50000"),
 "income_from_snapshot": base(snap_income="70000", monthly_income="0", monthly_loan_payments="0", purpose="Monthly Debt: 7000"),
 "debt_junk_falls_to_zero": base(monthly_income="60000", purpose="Monthly Debt: lots"),
 "nw_negative": base(snap_net_worth="-1500"), "nw_zero_none": base(snap_net_worth="0", total_net_worth="0"),
 "nw_499999": base(total_net_worth="499999.99"), "nw_500000": base(total_net_worth="500000"),
 "nw_999999": base(total_net_worth="999999"), "nw_1M": base(total_net_worth="1000000"), "nw_5M": base(total_net_worth="5000000"),
 "risk_19": base(risk_score=19), "risk_20": base(risk_score=20), "risk_39": base(risk_score=39), "risk_40": base(risk_score=40),
 "risk_59": base(risk_score=59), "risk_60": base(risk_score=60), "risk_zero": base(risk_score=0),
 "age_zero": base(client_age=0), "years_zero": base(years_of_employment="0"),
 "kyc_pending": base(kyc_status="pending", monthly_income="1"),
 "loans_all_zero": base(mortgage_balance="0", personal_loan_balance="0"),
 "loans_mixed": base(mortgage_balance="1500000.50", vehicle_loan_balance="250000.25"),
 "tie_dsr_11_25": base(amount="120000", preferred_term_months=12, monthly_income="80000", monthly_loan_payments="9000"),
 "unit_trust_profile": base(product_type="unit_trust", product_answers={"risk_appetite": "moderate", "investment_horizon": "5y",
     "liquidity": "", "withdrawal": "quarterly", "investment_style": "growth", "target_amount": "500000",
     "monthly_contribution": "abc", "objective": "retirement", "fund_type": "balanced"}),
 "savings_plan_numbers": base(product_type="savings_plan", product_answers={"target_amount": 25000.5, "monthly_contribution": True,
     "flexibility": "high", "liquidity": 0, "withdrawal": None}),
 "equities_blank_numbers": base(product_type="equities", product_answers={"target_amount": "", "monthly_contribution": None, "risk_appetite": "high"}),
 "credit_ignores_answers": base(product_type="credit_card", product_answers={"risk_appetite": "high", "target_amount": "10"}),
 "fixed_deposit_empty_answers": base(product_type="fixed_deposit", product_answers={}),
}


def random_case(rng: random.Random) -> dict:
    def money(lo, hi, p_none=0.15, p_zero=0.08):
        x = rng.random()
        if x < p_none: return None
        if x < p_none + p_zero: return "0"
        return f"{rng.randint(lo, hi)}" + (f".{rng.randint(0, 99):02d}" if rng.random() < 0.4 else "")
    parts = []
    for key in ["Purpose", "Property Type", "Property Value", "Vehicle Make", "Vehicle Type", "Vehicle Value", "Monthly Debt"]:
        if rng.random() < 0.45:
            val = rng.choice(["Home", "Land", "Apartment", "Toyota", "SUV", str(rng.randint(0, 6000000)), "1e5", "abc", "", "12,000"])
            parts.append(f" {key if rng.random() < .7 else key.lower()} : {val} ")
    rng.shuffle(parts)
    pt = rng.choice(TYPES + ["home_loan", "auto", "car_loan", "business"])
    pa = {}
    if rng.random() < 0.7:
        pa = {k: rng.choice(["", "x", "10", "2500.75", "abc", 5, 0, True, None]) for k in
              rng.sample(["risk_appetite", "investment_horizon", "liquidity", "withdrawal", "flexibility", "investment_style",
                          "target_amount", "monthly_contribution", "objective", "extra"], rng.randint(0, 6))}
    return base(product_type=pt, amount=money(1000, 9000000, 0, 0) or "1000", preferred_term_months=rng.choice([None, 0, 6, 12, 24, 36, 60, 120, 240]),
        purpose=("|".join(parts) if parts else rng.choice([None, ""])), product_answers=pa,
        kyc_status=rng.choice(["verified", "pending", None, "rejected"]), client_age=rng.choice([None, 0, 21, 34, 58]),
        employment_status=rng.choice([None, "employed", "self_employed"]), employment_type=rng.choice([None, "permanent"]),
        monthly_income=money(1000, 400000), total_net_worth=money(-500000, 8000000), has_existing_loans=rng.choice([None, True, False]),
        health_score=rng.choice([None, 10, 55, 90]), risk_score=rng.choice([None, 0, 19, 20, 39, 40, 59, 60, 95]),
        affordability_score=rng.choice([None, 33, 80]), years_of_employment=money(0, 40, 0.3, 0.2),
        monthly_loan_payments=money(500, 90000), snap_income=money(1000, 400000), snap_net_worth=money(-500000, 8000000),
        mortgage_balance=money(1000, 3000000), personal_loan_balance=money(1000, 900000), credit_card_balance=money(100, 90000),
        vehicle_loan_balance=money(1000, 1500000),
        loan_breakdown=rng.choice([None, [{"type": "car", "outstanding": 100.5, "monthly": None, "bank": "B", "months_left": 3}]]))


def main(out: str) -> None:
    os.makedirs(out, exist_ok=True)
    rng = random.Random(20261002)
    rows = [(n, r) for n, r in HAND.items()] + [(f"random_{i:03d}", random_case(rng)) for i in range(300)]
    cases = []
    for name, r in rows:
        text = emit(r)
        cases.append({"name": name, "input": json.loads(text, parse_float=lambda s: json.Number(s) if False else float(s)),
                      "input_json": text, "expected": run_builder(text)})
    # inputs are kept as raw JSON text so the DB parses the exact same decimals Python used
    with open(os.path.join(out, "cases.json"), "w") as f:
        f.write("{\"cases\":[" + ",\n".join(json.dumps({"name": c["name"], "expected": c["expected"]})[:-1] + ",\"input\":" + c["input_json"] + "}" for c in cases) + "]}\n")
    # rounding: exact binary ties, decimal-looking values, ratio-shaped values, random
    xs = [m_ / 4 for m_ in range(-8, 400)] + [k / 100 for k in range(5, 400, 10)]
    r2 = random.Random(7)
    xs += [r2.uniform(0, 300) for _ in range(800)] + [(r2.randint(0, 250000) / r2.randint(1000, 90000)) * 100 for _ in range(1500)]
    with open(os.path.join(out, "rounding.json"), "w") as f:
        json.dump({"cases": [[repr(x), repr(round(x, 1))] for x in xs]}, f)
    print(f"{len(cases)} builder cases, {len(xs)} rounding cases -> {out}")


if __name__ == "__main__":
    main(sys.argv[1])
