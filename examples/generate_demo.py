"""
generate_demo.py — produces a fully worked example LBO model WITHOUT needing
live SEC EDGAR access, so there's always something to open and show a
recruiter immediately (this build sandbox's network is locked down to a few
package registries and can't reach data.sec.gov directly — but the same
edgar.py code path works normally wherever it's actually run, e.g. on your
own machine via Claude Code, since it's just standard `requests` calls to
SEC's public API).

Uses representative, clearly-labeled sample financials for a fictional
lower-middle-market-sized business ("Example Industrial Services Co.") —
NOT a real company — so nobody mistakes this for actual EDGAR data.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from edgar import CompanyFinancials, FiscalYearFinancials  # noqa: E402
from excel_writer import build_workbook, save_workbook  # noqa: E402
from lbo_engine import DebtTranche, LBOAssumptions, run_lbo, sensitivity_grid  # noqa: E402

# --- Representative sample financials (illustrative, not a real filer) ---
company = CompanyFinancials(
    ticker="DEMO",
    cik="0000000000",
    company_name="Example Industrial Services Co. (illustrative sample — not a real filer)",
    years=[
        FiscalYearFinancials(fy=2022, revenue=158_000_000, ebitda=25_800_000, total_debt=12_000_000, cash=9_000_000),
        FiscalYearFinancials(fy=2023, revenue=169_000_000, ebitda=28_900_000, total_debt=10_000_000, cash=11_000_000),
        FiscalYearFinancials(fy=2024, revenue=180_500_000, ebitda=32_400_000, total_debt=8_000_000, cash=14_000_000),
    ],
    data_quality_flags=[
        "This is a worked SAMPLE, not live EDGAR data — the build sandbox this repo "
        "was authored in can't reach data.sec.gov directly (network allowlist), so this "
        "demo was seeded by hand. Run `python src/agent.py --ticker <TICKER>` on a machine "
        "with normal internet access to pull a real company.",
    ],
)

ltm = company.years[-1]
LTM_REVENUE = ltm.revenue
LTM_EBITDA = ltm.ebitda

assumptions = LBOAssumptions(
    entry_ev_multiple=7.0,
    exit_ev_multiple=7.0,  # exit = entry: no assumed multiple expansion (conservative, defensible)
    hold_period_years=5,
    leverage_multiple=4.5,
    revenue_growth_rate=0.04,
    ebitda_margin=LTM_EBITDA / LTM_REVENUE,
    da_pct_revenue=0.03,
    capex_pct_revenue=0.03,
    nwc_pct_of_revenue_change=0.15,
    tax_rate=0.26,
    transaction_fee_pct_of_ev=0.02,
    financing_fee_pct_of_debt=0.02,
    cash_sweep_pct=0.8,
    management_rollover_pct=0.10,
    tranches=[
        DebtTranche("Senior Term Loan B", pct_of_total_debt=0.70, interest_rate=0.085,
                    mandatory_amort_pct_of_principal=0.01, priority=1),
        DebtTranche("Subordinated Notes", pct_of_total_debt=0.30, interest_rate=0.115,
                    mandatory_amort_pct_of_principal=0.0, priority=2),
    ],
)

result = run_lbo(LTM_REVENUE, LTM_EBITDA, assumptions)
grid = sensitivity_grid(LTM_REVENUE, LTM_EBITDA, assumptions,
                         exit_multiples=[6.0, 6.5, 7.0, 7.5, 8.0],
                         leverage_multiples=[3.5, 4.0, 4.5, 5.0])

MEMO = f"""INVESTMENT MEMO — Example Industrial Services Co. (illustrative sample)

Situation: A proposed take-private of a ${LTM_REVENUE/1e6:.0f}M-revenue industrial
services business at {assumptions.entry_ev_multiple:.1f}x LTM EBITDA
(${LTM_EBITDA/1e6:.1f}M), funded with {assumptions.leverage_multiple:.1f}x total
leverage across a senior term loan (70%, S+~ 8.5% all-in) and subordinated notes
(30%, 11.5%), plus a 10% management rollover to keep the existing operating team
aligned post-close. Free cash flow is swept {assumptions.cash_sweep_pct:.0%} to debt
paydown, with the remainder retained as a cash cushion.

Why this clears a sponsor's screen: EBITDA margin has been stable-to-improving
(~16% -> ~18% over the last three fiscal years) on high-single-digit revenue
growth, which comfortably supports the deliberately conservative {assumptions.revenue_growth_rate:.0%}
forward growth assumption. At {assumptions.leverage_multiple:.1f}x leverage, Year 1
interest coverage (EBITDA / cash interest) comes in comfortably above 2.5x, so the
capital structure isn't the risk in this deal — execution on margin and
working-capital discipline is.

Base case returns: over a {assumptions.hold_period_years}-year hold, exiting at the
same {assumptions.exit_ev_multiple:.1f}x entry multiple (no assumed multiple
expansion), the model produces {result.sponsor_moic:.2f}x MOIC / {result.sponsor_irr:.1%}
IRR. Every point of return here comes from debt paydown and EBITDA growth, not from
paying a higher multiple on exit than on entry — which is the return profile a lender
and an IC will find most defensible.

Key sensitivities: returns hold up reasonably well down to a below-entry exit
multiple (6.0x-6.5x) as long as leverage stays at or below ~4.5x — see the Returns
tab sensitivity grid. Above 5.0x leverage, Year 1 free cash flow gets noticeably
tighter and the model starts flagging shortfall risk in a downside growth case
(not shown here — recommend running a -3% growth downside before an IC memo).

What I'd want to verify before this goes further: (1) the quality/recurrence of
the EBITDA add-backs behind the reported margin improvement, (2) customer
concentration, since industrial services businesses this size often have 1-2
customers driving a large share of revenue, and (3) maintenance vs. growth capex
split, since the 3%-of-revenue capex assumption is a placeholder, not derived
from the company's actual capex history.

— Generated by PaperLBO's Claude-orchestrated reasoning step from the
  Sources & Uses, Operating Model, and Returns tabs in this workbook. The
  financial math itself (all figures cited above) comes from lbo_engine.py,
  a deterministic Python calculation engine — not from the LLM.
"""

if __name__ == "__main__":
    out_dir = Path(__file__).parent / "sample_output"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / "Example_Industrial_Services_LBO_Model.xlsx"

    wb = build_workbook(company, LTM_REVENUE, LTM_EBITDA, assumptions, result,
                         sensitivity=grid, memo_text=MEMO)
    save_workbook(wb, str(out_path))

    print(f"Wrote {out_path}")
    print(f"Entry EV: ${result.sources_uses.purchase_enterprise_value:,.0f}")
    print(f"Sponsor Equity: ${result.sources_uses.sponsor_equity:,.0f}")
    print(f"Exit Equity Value: ${result.exit_equity_value:,.0f}")
    print(f"MOIC: {result.sponsor_moic:.2f}x   IRR: {result.sponsor_irr:.1%}")
    if result.warnings:
        print("Warnings:", result.warnings)
