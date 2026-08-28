"""
Unit tests for lbo_engine.py, checked against hand-calculated expected values.

Base case (see comment above test_single_year_matches_hand_calculation for the
full hand math): $100 revenue, 20% EBITDA margin, 6x entry & exit multiple,
3x leverage, single 10% interest tranche, no fees/growth/taxes/capex/D&A,
100% cash sweep, 1-year hold.

  Purchase EV        = 6 * 20                = 120
  New Debt            = 3 * 20                = 60
  Sponsor Equity       = 120 - 60              = 60
  Year 1 interest      = 60 * 10%              = 6
  Year 1 FCF           = 20 - 6                = 14   (no capex/D&A/tax/nwc)
  Debt swept           = 14  -> ending debt     = 46
  Exit EV              = 6 * 20                = 120
  Exit equity value    = 120 - 46              = 74
  MOIC                 = 74 / 60               = 1.2333...
  IRR (1yr)            = 1.2333 - 1            = 23.33%
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from lbo_engine import DebtTranche, LBOAssumptions, run_lbo, sensitivity_grid  # noqa: E402

BASE_TRANCHE = [DebtTranche("Term Loan", pct_of_total_debt=1.0, interest_rate=0.10,
                             mandatory_amort_pct_of_principal=0.0, priority=1)]


def base_assumptions(**overrides) -> LBOAssumptions:
    defaults = dict(
        entry_ev_multiple=6.0,
        exit_ev_multiple=6.0,
        hold_period_years=1,
        leverage_multiple=3.0,
        revenue_growth_rate=0.0,
        ebitda_margin=0.20,
        da_pct_revenue=0.0,
        capex_pct_revenue=0.0,
        nwc_pct_of_revenue_change=0.0,
        tax_rate=0.0,
        transaction_fee_pct_of_ev=0.0,
        financing_fee_pct_of_debt=0.0,
        cash_sweep_pct=1.0,
        management_rollover_pct=0.0,
        tranches=BASE_TRANCHE,
    )
    defaults.update(overrides)
    return LBOAssumptions(**defaults)


def test_sources_and_uses_matches_hand_calculation():
    result = run_lbo(ltm_revenue=100, ltm_ebitda=20, assumptions=base_assumptions())
    su = result.sources_uses
    assert su.purchase_enterprise_value == 120
    assert su.total_new_debt == 60
    assert su.sponsor_equity == 60
    assert su.total_uses == 120
    assert abs(su.total_sources - su.total_uses) < 1e-9, "sources must equal uses"


def test_single_year_matches_hand_calculation():
    result = run_lbo(ltm_revenue=100, ltm_ebitda=20, assumptions=base_assumptions())
    y1 = result.projections[0]
    assert y1.total_interest_expense == 6.0
    assert y1.levered_free_cash_flow_pre_sweep == 14.0
    assert y1.cash_swept == 14.0
    assert y1.total_debt_ending == 46.0

    assert result.exit_enterprise_value == 120.0
    assert result.exit_net_debt == 46.0
    assert result.exit_equity_value == 74.0
    assert abs(result.sponsor_moic - (74 / 60)) < 1e-9
    assert abs(result.sponsor_irr - (74 / 60 - 1)) < 1e-9


def test_two_year_hold_compounds_and_irr_is_geometric_mean():
    result = run_lbo(ltm_revenue=100, ltm_ebitda=20, assumptions=base_assumptions(hold_period_years=2))
    moic = result.sponsor_moic
    irr = result.sponsor_irr
    assert abs((1 + irr) ** 2 - moic) < 1e-9, "IRR must compound to MOIC over the hold period"


def test_debt_paydown_cannot_go_negative_even_with_huge_fcf():
    # Force FCF far larger than the debt balance to confirm the sweep clamps at zero.
    result = run_lbo(ltm_revenue=100, ltm_ebitda=90, assumptions=base_assumptions(
        entry_ev_multiple=1.0, leverage_multiple=0.5, exit_ev_multiple=1.0,
        ebitda_margin=0.90, tranches=[DebtTranche("Term Loan", 1.0, 0.01, 0.0, 1)],
    ))
    assert result.projections[0].total_debt_ending == 0.0
    assert result.projections[0].total_debt_ending >= 0.0


def test_shortfall_flag_trips_when_interest_exceeds_ebitda():
    # Extreme leverage: interest expense alone exceeds EBITDA -> FCF must go negative pre-sweep.
    result = run_lbo(ltm_revenue=100, ltm_ebitda=20, assumptions=base_assumptions(
        leverage_multiple=8.0, tranches=[DebtTranche("Term Loan", 1.0, 0.30, 0.0, 1)],
    ))
    assert result.projections[0].shortfall_flag is True
    assert any("negative" in w for w in result.warnings)


def test_senior_tranche_paid_before_subordinated_in_sweep():
    tranches = [
        DebtTranche("Senior", pct_of_total_debt=0.5, interest_rate=0.08, priority=1),
        DebtTranche("Sub", pct_of_total_debt=0.5, interest_rate=0.12, priority=2),
    ]
    result = run_lbo(ltm_revenue=100, ltm_ebitda=20, assumptions=base_assumptions(
        leverage_multiple=1.0, tranches=tranches,
    ))
    y1 = result.projections[0]
    senior_orig = 0.5 * 20  # leverage 1.0x EBITDA(20) = 20 total debt, 50% senior = 10
    sub_orig = 0.5 * 20
    # with plenty of FCF the senior tranche should be paid down before the sub tranche is touched
    assert y1.tranche_ending_balances["Senior"] <= senior_orig
    if y1.tranche_ending_balances["Senior"] > 0:
        assert y1.tranche_ending_balances["Sub"] == sub_orig, \
            "sub tranche shouldn't be swept until senior is fully repaid"


def test_unswept_cash_is_retained_not_destroyed():
    # Same base case as the hand-calc, but sweep only 50%. Hand math:
    #   Year 1 FCF = 14, swept = 14 * 50% = 7  -> debt 60 -> 53
    #   retained cash = 14 - 7 = 7  (this must NOT vanish)
    #   exit net debt = 53 - 7 = 46  -> identical to the 100%-sweep case
    # i.e. a dollar of FCF pays down debt OR builds cash; both cut net debt equally,
    # so within a single year the sweep % must not change exit equity value.
    result = run_lbo(ltm_revenue=100, ltm_ebitda=20, assumptions=base_assumptions(cash_sweep_pct=0.5))
    y1 = result.projections[0]
    assert y1.cash_swept == 7.0
    assert y1.total_debt_ending == 53.0
    assert y1.cash_balance_ending == 7.0
    assert result.exit_cash_balance == 7.0
    assert result.exit_net_debt == 46.0
    assert result.exit_equity_value == 74.0  # same as the 100%-sweep hand calc — no cash destroyed


def test_lower_sweep_never_beats_full_sweep_over_multiyear_hold():
    # Over multiple years, a lower sweep leaves more debt outstanding for longer, so more
    # interest is paid and returns should be <= the full-sweep case (never higher).
    full = run_lbo(100, 20, base_assumptions(hold_period_years=5, cash_sweep_pct=1.0))
    partial = run_lbo(100, 20, base_assumptions(hold_period_years=5, cash_sweep_pct=0.7))
    assert partial.sponsor_irr <= full.sponsor_irr + 1e-9
    assert partial.sponsor_moic <= full.sponsor_moic + 1e-9


def test_sensitivity_grid_shape():
    grid = sensitivity_grid(
        ltm_revenue=100, ltm_ebitda=20, base=base_assumptions(),
        exit_multiples=[5.0, 6.0, 7.0], leverage_multiples=[2.0, 3.0],
    )
    assert len(grid["irr"]) == 2  # one row per leverage multiple
    assert len(grid["irr"][0]) == 3  # one col per exit multiple
    assert len(grid["moic"]) == 2 and len(grid["moic"][0]) == 3


if __name__ == "__main__":
    import traceback
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    passed, failed = 0, 0
    for t in tests:
        try:
            t()
            passed += 1
        except Exception:
            failed += 1
            print(f"FAILED: {t.__name__}")
            traceback.print_exc()
    print(f"\n{passed} passed, {failed} failed")
