"""
lbo_engine.py — deterministic LBO math. No LLM involved anywhere in this file.

This is the part of the project that has to be exactly right, so it's plain
Python with unit tests (see tests/test_lbo_engine.py), not something an LLM
free-forms. The Claude agent's job (agent.py) is to choose sensible inputs
and explain the output in plain English — never to compute the numbers.

Model conventions (standard "quick" / paper-LBO conventions):
  - Deal is cash-free / debt-free: the target's existing cash & debt are not
    inherited — the sponsor pays Enterprise Value and puts a fresh capital
    structure in place at close.
  - Flat revenue growth rate and flat EBITDA margin over the hold period,
    unless per-year overrides are supplied.
  - No interim dividends/recaps — all value is realized at a single exit.
  - IRR with a single entry and single exit cash flow simplifies to
    MOIC ** (1/years) - 1; we still expose money-weighted cash flows so a
    dividend recap or partial-sale extension is a small change later.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class DebtTranche:
    name: str  # e.g. "Senior Term Loan", "Subordinated Notes"
    pct_of_total_debt: float  # 0-1, must sum to 1 across all tranches
    interest_rate: float  # annual, e.g. 0.09 for 9%
    mandatory_amort_pct_of_principal: float = 0.0  # e.g. 0.01 = 1%/yr of original principal
    priority: int = 1  # 1 = paid down first in the cash sweep (senior), higher = more junior


@dataclass
class LBOAssumptions:
    entry_ev_multiple: float  # x LTM EBITDA
    exit_ev_multiple: float  # x exit-year EBITDA
    hold_period_years: int
    leverage_multiple: float  # total new debt, x LTM EBITDA
    revenue_growth_rate: float  # flat annual %, e.g. 0.05
    ebitda_margin: Optional[float] = None  # None = hold at entry LTM margin
    da_pct_revenue: float = 0.02
    capex_pct_revenue: float = 0.02
    nwc_pct_of_revenue_change: float = 0.10  # cash used per $1 of revenue growth
    tax_rate: float = 0.25
    transaction_fee_pct_of_ev: float = 0.02
    financing_fee_pct_of_debt: float = 0.02
    cash_sweep_pct: float = 0.8  # fraction of post-mandatory-amort FCF swept to debt paydown;
    # 0.8 (not 100%) is the more realistic default — real deals sweep ~75-90% and let the rest
    # build a cash cushion. Retained FCF is accumulated as cash and nets against debt at exit,
    # so a sub-100% sweep stays internally consistent (the un-swept cash doesn't disappear).
    management_rollover_pct: float = 0.0  # fraction of purchase equity value rolled by sellers
    tranches: list[DebtTranche] = field(default_factory=lambda: [
        DebtTranche("Senior Term Loan", pct_of_total_debt=0.75, interest_rate=0.085,
                    mandatory_amort_pct_of_principal=0.01, priority=1),
        DebtTranche("Subordinated Notes", pct_of_total_debt=0.25, interest_rate=0.115,
                    mandatory_amort_pct_of_principal=0.0, priority=2),
    ])

    def __post_init__(self):
        total = sum(t.pct_of_total_debt for t in self.tranches)
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"Debt tranche percentages must sum to 1.0, got {total}")


@dataclass
class SourcesUses:
    purchase_enterprise_value: float
    transaction_fees: float
    financing_fees: float
    total_uses: float
    total_new_debt: float
    management_rollover: float
    sponsor_equity: float
    total_sources: float


@dataclass
class YearProjection:
    year: int
    revenue: float
    ebitda: float
    ebitda_margin: float
    d_and_a: float
    ebit: float
    total_interest_expense: float
    cash_taxes: float
    capex: float
    nwc_change: float
    levered_free_cash_flow_pre_sweep: float
    mandatory_amortization: float
    cash_available_for_sweep: float
    cash_swept: float
    cash_balance_ending: float  # retained (un-swept) FCF accumulates here; nets against debt at exit
    tranche_ending_balances: dict[str, float]
    total_debt_ending: float
    shortfall_flag: bool = False  # FCF was negative before mandatory amort — real red flag


@dataclass
class LBOResult:
    sources_uses: SourcesUses
    projections: list[YearProjection]
    exit_ebitda: float
    exit_enterprise_value: float
    exit_cash_balance: float
    exit_net_debt: float
    exit_equity_value: float
    sponsor_moic: float
    sponsor_irr: float
    warnings: list[str] = field(default_factory=list)


def run_lbo(ltm_revenue: float, ltm_ebitda: float, assumptions: LBOAssumptions) -> LBOResult:
    if ltm_ebitda <= 0:
        raise ValueError("LTM EBITDA must be positive to run a standard leveraged LBO")

    warnings: list[str] = []
    entry_margin = ltm_ebitda / ltm_revenue if ltm_revenue else 0.0
    margin = assumptions.ebitda_margin if assumptions.ebitda_margin is not None else entry_margin

    # --- Sources & Uses ---
    purchase_ev = assumptions.entry_ev_multiple * ltm_ebitda
    transaction_fees = assumptions.transaction_fee_pct_of_ev * purchase_ev
    total_new_debt = assumptions.leverage_multiple * ltm_ebitda
    financing_fees = assumptions.financing_fee_pct_of_debt * total_new_debt
    total_uses = purchase_ev + transaction_fees + financing_fees

    purchase_equity_value = purchase_ev  # cash-free/debt-free convention: EV ~ equity purchase price here
    management_rollover = assumptions.management_rollover_pct * purchase_equity_value
    sponsor_equity = total_uses - total_new_debt - management_rollover
    if sponsor_equity < 0:
        warnings.append(
            "Leverage + rollover exceed total uses — sponsor equity would be negative. "
            "Lower the leverage multiple or entry multiple."
        )

    total_sources = total_new_debt + management_rollover + sponsor_equity
    su = SourcesUses(
        purchase_enterprise_value=purchase_ev,
        transaction_fees=transaction_fees,
        financing_fees=financing_fees,
        total_uses=total_uses,
        total_new_debt=total_new_debt,
        management_rollover=management_rollover,
        sponsor_equity=sponsor_equity,
        total_sources=total_sources,
    )

    # initialize tranche balances by pct of total new debt
    tranche_balances = {t.name: t.pct_of_total_debt * total_new_debt for t in assumptions.tranches}
    tranche_original_principal = dict(tranche_balances)
    sorted_tranches = sorted(assumptions.tranches, key=lambda t: t.priority)

    projections: list[YearProjection] = []
    revenue = ltm_revenue
    cash_balance = 0.0  # cash-free/debt-free entry: sponsor inherits no cash, builds it from retained FCF
    for year in range(1, assumptions.hold_period_years + 1):
        revenue = revenue * (1 + assumptions.revenue_growth_rate)
        ebitda = revenue * margin
        d_and_a = revenue * assumptions.da_pct_revenue
        ebit = ebitda - d_and_a
        capex = revenue * assumptions.capex_pct_revenue
        nwc_change = (revenue - (revenue / (1 + assumptions.revenue_growth_rate))) * \
            assumptions.nwc_pct_of_revenue_change

        total_interest = sum(bal * t.interest_rate for t, bal in
                              zip(assumptions.tranches, [tranche_balances[t.name] for t in assumptions.tranches]))
        pretax_income = ebit - total_interest
        cash_taxes = max(0.0, pretax_income * assumptions.tax_rate)

        fcf_pre_sweep = ebitda - capex - nwc_change - cash_taxes - total_interest
        shortfall = fcf_pre_sweep < 0

        # mandatory amortization first, capped at remaining balance
        mandatory_amort_total = 0.0
        for t in sorted_tranches:
            sched = t.mandatory_amort_pct_of_principal * tranche_original_principal[t.name]
            sched = min(sched, tranche_balances[t.name])
            tranche_balances[t.name] -= sched
            mandatory_amort_total += sched

        cash_for_sweep = max(0.0, fcf_pre_sweep - mandatory_amort_total) * assumptions.cash_sweep_pct
        swept_total = 0.0
        remaining_sweep = cash_for_sweep
        for t in sorted_tranches:
            if remaining_sweep <= 0:
                break
            pay = min(remaining_sweep, tranche_balances[t.name])
            tranche_balances[t.name] -= pay
            remaining_sweep -= pay
            swept_total += pay

        total_debt_ending = sum(tranche_balances.values())

        # Whatever FCF (after mandatory amort) wasn't swept to debt stays as cash — either
        # because cash_sweep_pct < 1.0, or because the debt was already fully repaid. This
        # keeps the model consistent: a dollar of FCF either pays down debt or builds cash,
        # and both reduce net debt at exit. (A negative here means a cash shortfall covered
        # implicitly — the shortfall_flag/warning already surfaces that case.)
        cash_balance += (fcf_pre_sweep - mandatory_amort_total) - swept_total

        projections.append(YearProjection(
            year=year,
            revenue=revenue,
            ebitda=ebitda,
            ebitda_margin=margin,
            d_and_a=d_and_a,
            ebit=ebit,
            total_interest_expense=total_interest,
            cash_taxes=cash_taxes,
            capex=capex,
            nwc_change=nwc_change,
            levered_free_cash_flow_pre_sweep=fcf_pre_sweep,
            mandatory_amortization=mandatory_amort_total,
            cash_available_for_sweep=max(0.0, fcf_pre_sweep - mandatory_amort_total),
            cash_swept=swept_total,
            cash_balance_ending=cash_balance,
            tranche_ending_balances=dict(tranche_balances),
            total_debt_ending=total_debt_ending,
            shortfall_flag=shortfall,
        ))
        if shortfall:
            warnings.append(f"Year {year}: free cash flow before debt paydown is negative — "
                             "this leverage level isn't serviceable with these assumptions.")

    exit_ebitda = projections[-1].ebitda
    exit_ev = assumptions.exit_ev_multiple * exit_ebitda
    exit_cash_balance = projections[-1].cash_balance_ending
    # Net debt = remaining gross debt less the cash the business accumulated over the hold.
    exit_net_debt = projections[-1].total_debt_ending - exit_cash_balance
    exit_equity_value = exit_ev - exit_net_debt

    if sponsor_equity > 0:
        moic = exit_equity_value / sponsor_equity
        irr = moic ** (1 / assumptions.hold_period_years) - 1 if moic > 0 else -1.0
    else:
        moic, irr = float("nan"), float("nan")

    if exit_equity_value < 0:
        warnings.append("Exit equity value is negative — the deal as structured loses money for the sponsor.")

    return LBOResult(
        sources_uses=su,
        projections=projections,
        exit_ebitda=exit_ebitda,
        exit_enterprise_value=exit_ev,
        exit_cash_balance=exit_cash_balance,
        exit_net_debt=exit_net_debt,
        exit_equity_value=exit_equity_value,
        sponsor_moic=moic,
        sponsor_irr=irr,
        warnings=warnings,
    )


def sensitivity_grid(ltm_revenue: float, ltm_ebitda: float, base: LBOAssumptions,
                      exit_multiples: list[float], leverage_multiples: list[float]) -> dict:
    """Classic PE output: IRR/MOIC grid across exit multiple x entry leverage."""
    grid = {"exit_multiples": exit_multiples, "leverage_multiples": leverage_multiples,
            "irr": [], "moic": []}
    for lev in leverage_multiples:
        irr_row, moic_row = [], []
        for exitm in exit_multiples:
            a = LBOAssumptions(**{**base.__dict__, "leverage_multiple": lev, "exit_ev_multiple": exitm})
            r = run_lbo(ltm_revenue, ltm_ebitda, a)
            irr_row.append(r.sponsor_irr)
            moic_row.append(r.sponsor_moic)
        grid["irr"].append(irr_row)
        grid["moic"].append(moic_row)
    return grid
