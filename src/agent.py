"""
agent.py — the actual "agent" part of this project.

This is a real Claude tool-use loop (not a single prompt): Claude decides
which tools to call, in what order, inspects the results, and can revise its
own assumptions if the first pass produces an unrealistic deal — then hands
off to deterministic Python for every number that ends up in the workbook.

Design principle: Claude orchestrates and writes English. lbo_engine.py does
every calculation. This keeps the numbers auditable — if a recruiter asks
"how do I know the LLM didn't just make up the IRR," the answer is: it
didn't touch it, it called a Python function.

Cost control (see docs/COST_CONTROL.md for the full picture):
  - Free data only: SEC EDGAR, no paid data API.
  - Hard cap on tool-use turns (MAX_TURNS) so a confused loop can't run away.
  - Model + max_tokens are both env-configurable so you can point this at a
    cheaper model for routine runs.
  - Usage (input/output tokens) is printed after every run so cost is never
    a surprise — check docs.claude.com/en/docs/about-claude/models and the
    Anthropic Console pricing page for current per-token rates for whichever
    model you configure.

Usage:
    export ANTHROPIC_API_KEY=...
    export EDGAR_USER_AGENT="Your Name your@email.com"
    python src/agent.py --ticker SHOE
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

import anthropic

sys.path.insert(0, str(Path(__file__).resolve().parent))

from benchmarks import get_industry_benchmarks  # noqa: E402
from edgar import CompanyFinancials, fetch_company_financials  # noqa: E402
from filings import get_management_discussion  # noqa: E402
from excel_writer import build_workbook, save_workbook  # noqa: E402
from lbo_engine import DebtTranche, LBOAssumptions, run_lbo, sensitivity_grid  # noqa: E402
from memo_audit import audit_memo, build_labelled_values, collect_values  # noqa: E402
from verify import verify_financials  # noqa: E402

# Default to Haiku 4.5 — the cheapest current model, and it produces a solid memo
# (~3.5 cents/run at typical token volumes). Bump to a stronger model via LBO_AGENT_MODEL
# for the polished version you send to a recruiter. See docs/COST_CONTROL.md.
MODEL = os.environ.get("LBO_AGENT_MODEL", "claude-haiku-4-5")
MAX_TOKENS = int(os.environ.get("LBO_AGENT_MAX_TOKENS", "3000"))
MAX_TURNS = int(os.environ.get("LBO_AGENT_MAX_TURNS", "12"))  # hard stop so a confused loop can't burn budget
# Raised from 8 when verify_financials / get_industry_benchmarks / get_management_discussion
# were added — a full run is now ~8 tool calls and 8 turns cut the memo off.

SYSTEM_PROMPT = """\
You are a junior private equity associate automating a "quick look" / paper LBO \
on a public company, using tools that fetch real SEC data and run a deterministic \
LBO calculation engine. You never compute financial results yourself — every number \
in your final answer must come from a tool result. Your job is judgment (choosing \
reasonable assumptions) and communication (explaining them in plain English), not math.

Workflow:
1. Call get_company_financials for the ticker the user gave you.
2. If EBITDA is missing, negative, or the data-quality flags say the data is bad, \
tell the user plainly that this isn't a clean LBO candidate and stop — do not force a model.
2a. verify_financials, get_industry_benchmarks and get_management_discussion do not \
depend on each other — call all three in the SAME turn rather than one per turn. Every \
extra turn re-sends the whole conversation, so batching them is materially cheaper.
2b. Call verify_financials. It cross-checks the figures you are about to model and \
resolves which PERIOD a fiscal-year label actually refers to. Treat its output as \
mandatory memo content, not optional colour:
   - If it reports a discrepancy, say so explicitly in the memo, give BOTH numbers, and \
state which one the model uses and why. Never silently pick one.
   - SEC labels a fiscal year by the year it STARTS. Retailers and other non-December \
year-ends are therefore labelled a full year apart from how most data providers label \
them. Whenever you cite a fiscal year for such a company, give the period END DATE \
alongside it (e.g. "FY2025, the year ended 2026-01-31"). A reader comparing your memo \
against a third-party source will otherwise think the number is wrong when it is not.
   - Its checks are all SEC-sourced, so they cannot detect an error in SEC's own data. \
Do not claim the figures are independently confirmed.
2c. Call get_industry_benchmarks with the company's sector to get real EV/EBITDA and \
margin averages for that industry. If the match looks wrong, retry once with an exact \
name from the list it returns.
3. Propose deal assumptions and justify each choice against the company's actual revenue \
size, margin, and growth from step 1 AND the sector benchmarks from step 2c:
   - Entry EV/EBITDA multiple: cite the benchmark range for this company's sector \
explicitly in your justification. Those benchmarks are PUBLIC TRADING multiples for \
minority stakes across a whole sector — an LBO entry price for control of one small, \
slow-growing company normally sits well BELOW them. So use the benchmark as the \
reference point you explain your entry multiple AGAINST; do not move the multiple up to \
match it. State the gap and the reason for it (scale, growth, margin, cyclicality). \
As a sanity range, 6-9x is typical for smaller, less differentiated businesses.
   - Leverage: roughly 3-4x EBITDA for smaller/cyclical/lower-margin businesses, \
4.5-5.5x for larger/stable/higher-margin ones. Never propose leverage that implies \
Year-1 interest expense anywhere near EBITDA.
   - Exit multiple: the BASE CASE always uses the SAME exit multiple as entry — no \
assumed multiple expansion, ever. This is the conservative, defensible convention and \
it is what the memo leads with. If you believe expansion is justified, you may model it, \
but only as a separately labelled UPSIDE case on top of the flat base case (see step 5b). \
Never present an expanded-exit result as the base case.
   - Hold period: 5 years unless there's a reason to pick something else.
   - Leave operating assumptions (D&A %, capex %, NWC %, tax rate, fees) at the tool's \
sensible defaults unless the company's data suggests otherwise.
   - Cash sweep: keep it around the 0.8 (80%) default. A 100% sweep overstates returns by \
assuming every dollar of free cash flow pays down debt with no cash cushion retained; ~80% \
is the more defensible convention.
4. Call propose_and_run_lbo with those assumptions.
5. Sanity-check the result. If there are warnings (cash flow shortfall, negative equity) \
or the IRR is outside a believable ~10-40% range, adjust ONE assumption and rerun ONCE. \
That adjustment must be to ENTRY MULTIPLE or LEVERAGE — never to the exit multiple. \
Raising the exit multiple to rescue a return is the single easiest way to make a bad \
deal look good, and it is exactly what a reader will check first. Don't iterate more \
than that — explain the tension instead of hiding it. If the flat-multiple base case \
simply doesn't clear a PE hurdle, SAY SO as the headline finding. That is a legitimate \
and useful answer, not a failure.
5b. OPTIONAL upside case: if there is a specific, stated reason multiple expansion could \
happen, run propose_and_run_lbo once more with the higher exit multiple. This is an \
UPSIDE SCENARIO, reported after and beneath the base case, and always labelled as \
assuming expansion from Nx to Mx.
6. Call run_sensitivity to build an IRR/MOIC grid across exit multiple and leverage.
6b. Call get_management_discussion with 2-4 topics you actually need explained (e.g. \
["gross margin", "comparable store sales", "outlook"]). Use what management themselves \
said to explain WHY the numbers moved. If it comes back unavailable, write the narrative \
without it and say the qualitative read is not sourced from the filing.
7. Write a short investment memo (structured like: situation, why it clears a screen, \
base-case returns, key sensitivities, what you'd want to verify before this goes further) \
using ONLY numbers that came back from the tools. Do not invent or round-trip numbers \
from memory. Specifically:
   - Every fiscal year you cite must carry its period end date if verify_financials \
flagged the label as ambiguous.
   - Every discrepancy verify_financials reported must appear in the memo with both values.
   - Attribute qualitative claims about WHY results moved to management's own discussion, \
or state plainly that it is your inference. Do not present an inferred explanation as fact.
   - Attach every dollar figure to the quantity it actually measures. Do not write EBITDA \
next to the word "cash", or any figure next to a label it does not belong to. A \
deterministic audit checks this before the file is written and will reject the memo.
   Structure the memo in this order, and do not deviate:
     SITUATION -> VERIFICATION NOTE -> SECTOR CONTEXT -> BASE CASE -> UPSIDE CASE \
(only if you ran one) -> SENSITIVITY -> MANAGEMENT DISCUSSION -> RISKS -> CONCLUSION
   - The BASE CASE section states the flat-multiple result (exit multiple = entry \
multiple) and its MOIC and IRR. This is THE headline number of the memo. It appears \
before any upside case and is what the CONCLUSION leads with.
   - Any result that relies on a higher exit multiple than entry goes in the UPSIDE CASE \
section, explicitly labelled, e.g. "Upside case — assumes exit multiple expands from \
5.0x to 7.0x, which the base case does not assume." A reader must never have to dig \
through the sensitivity grid to discover that the headline return needed expansion.
8b. If write_excel_and_memo returns a figure-audit failure, the file was NOT written. \
Fix every figure it lists and call it again with the corrected memo — do not argue with \
the audit or repeat the same text.
8. Call write_excel_and_memo with that memo text to produce the final workbook.
9. Give the user a short plain-English summary and tell them where the file is.

Be honest about weak data or a bad-fit ticker rather than forcing a polished-looking \
model on top of shaky numbers — that honesty is the whole point of this tool.
"""

TOOLS = [
    {
        "name": "get_company_financials",
        "description": "Fetch the last several years of 10-K annual financials for a US public "
                        "company from SEC EDGAR (free, official data) and compute LTM EBITDA.",
        "input_schema": {
            "type": "object",
            "properties": {"ticker": {"type": "string", "description": "Stock ticker, e.g. SHOE"}},
            "required": ["ticker"],
        },
    },
    {
        "name": "verify_financials",
        "description": "Independently cross-check the LTM figures from the last "
                        "get_company_financials call before they go into a model. Resolves "
                        "which calendar period a fiscal-year label actually covers (SEC labels "
                        "a fiscal year by the year it STARTS, which is a full year off from how "
                        "most data providers label non-December year-ends), compares revenue "
                        "across alternate XBRL tags, recomputes EBITDA by a second independent "
                        "path, and checks whether the figure was restated between filings. "
                        "Returns both numbers and a flag for anything more than ~5% apart. "
                        "All checks are SEC-sourced, so they cannot detect an error in SEC's own "
                        "data. Best-effort: may return status 'unavailable'.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_industry_benchmarks",
        "description": "Look up sector-average EV/EBITDA, EV/EBIT and margins (including "
                        "EBITDA/Sales) for a US industry, from NYU Stern's Damodaran datasets "
                        "(free, updated annually). Use this to ground entry/exit multiple "
                        "assumptions in real sector data. NOTE these are public-market trading "
                        "multiples for minority stakes, not LBO entry multiples — a control "
                        "buyout of a small, low-growth company normally prices well below them. "
                        "If the matched_industry it returns looks wrong, call again with an "
                        "exact name from available_industries. Best-effort: may return "
                        "status 'unavailable'.",
        "input_schema": {
            "type": "object",
            "properties": {"industry": {"type": "string",
                                         "description": "Sector name, e.g. 'Retail (Special Lines)' "
                                                        "or 'specialty retail'"}},
            "required": ["industry"],
        },
    },
    {
        "name": "get_management_discussion",
        "description": "Fetch short excerpts of Management's Discussion & Analysis (Item 7) from "
                        "the company's latest 10-K, via SEC full-text search. Use it so the memo's "
                        "qualitative narrative quotes or paraphrases what management actually said "
                        "drove results, instead of an inferred explanation. Best-effort: may "
                        "return status 'unavailable' or 'not_found'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "topics": {"type": "array", "items": {"type": "string"},
                            "description": "2-4 phrases to find in the MD&A, e.g. "
                                           "['gross margin', 'comparable store sales', 'outlook']"},
            },
            "required": ["topics"],
        },
    },
    {
        "name": "propose_and_run_lbo",
        "description": "Run the deterministic LBO calculation engine against the most recently "
                        "fetched company's financials, given a full set of deal assumptions. "
                        "Returns sources & uses, year-by-year projections, exit value, MOIC, IRR, "
                        "and any warnings (e.g. negative free cash flow, negative equity value).",
        "input_schema": {
            "type": "object",
            "properties": {
                "entry_ev_multiple": {"type": "number"},
                "exit_ev_multiple": {"type": "number"},
                "hold_period_years": {"type": "integer"},
                "leverage_multiple": {"type": "number"},
                "revenue_growth_rate": {"type": "number"},
                "da_pct_revenue": {"type": "number", "default": 0.03},
                "capex_pct_revenue": {"type": "number", "default": 0.03},
                "nwc_pct_of_revenue_change": {"type": "number", "default": 0.15},
                "tax_rate": {"type": "number", "default": 0.25},
                "transaction_fee_pct_of_ev": {"type": "number", "default": 0.02},
                "financing_fee_pct_of_debt": {"type": "number", "default": 0.02},
                "cash_sweep_pct": {"type": "number", "default": 0.8},
                "management_rollover_pct": {"type": "number", "default": 0.0},
                "senior_pct_of_debt": {"type": "number", "default": 0.75},
                "senior_rate": {"type": "number", "default": 0.085},
                "senior_mandatory_amort_pct": {"type": "number", "default": 0.01},
                "sub_rate": {"type": "number", "default": 0.115},
            },
            "required": ["entry_ev_multiple", "exit_ev_multiple", "hold_period_years",
                          "leverage_multiple", "revenue_growth_rate"],
        },
    },
    {
        "name": "run_sensitivity",
        "description": "Build an IRR/MOIC sensitivity grid across exit multiple and leverage, "
                        "using the same base assumptions as the last propose_and_run_lbo call.",
        "input_schema": {
            "type": "object",
            "properties": {
                "exit_multiples": {"type": "array", "items": {"type": "number"}},
                "leverage_multiples": {"type": "array", "items": {"type": "number"}},
            },
            "required": ["exit_multiples", "leverage_multiples"],
        },
    },
    {
        "name": "write_excel_and_memo",
        "description": "Write the final formatted Excel LBO model to disk, including the "
                        "investment memo text on its own tab. Call this last.",
        "input_schema": {
            "type": "object",
            "properties": {
                "memo_text": {"type": "string"},
                "output_filename": {"type": "string",
                                     "description": "e.g. TICKER_LBO_Model.xlsx"},
            },
            "required": ["memo_text", "output_filename"],
        },
    },
]


class AgentState:
    """Holds the working data between tool calls within one run."""
    def __init__(self):
        self.company: CompanyFinancials | None = None
        self.ltm_revenue: float | None = None
        self.ltm_ebitda: float | None = None
        self.assumptions: LBOAssumptions | None = None
        self.result = None
        # Every propose_and_run_lbo call, in order. The agent may run a flat base case
        # and then an upside case with an expanded exit multiple; without this, the LAST
        # run silently became the headline, the workbook, and the sensitivity centre —
        # so a memo leading with the base case could ship an upside-case model.
        self.runs: list[dict] = []
        self.sensitivity: dict | None = None
        self.saved_to: str | None = None
        self.memo_text: str | None = None
        self.tool_values: set = set()      # every number any tool returned, for the memo audit
        self.memo_audit: dict | None = None
        self.memo_audit_attempts: int = 0
        self.verification: dict | None = None
        self.benchmarks: dict | None = None
        self.mdna: dict | None = None


def base_run(state) -> dict | None:
    """
    The base case is the most recent run with NO multiple expansion (exit == entry).
    That is the conservative convention the memo leads with, so it is what the
    workbook, the headline returns and the sensitivity grid are built from.
    Returns None when the agent never ran a flat case.
    """
    flat = [r for r in state.runs
            if r["assumptions"].exit_ev_multiple == r["assumptions"].entry_ev_multiple]
    return flat[-1] if flat else None


def upside_runs(state) -> list[dict]:
    """Runs that assume the exit multiple expands above entry — never the headline."""
    return [r for r in state.runs
            if r["assumptions"].exit_ev_multiple > r["assumptions"].entry_ev_multiple]


def _assumptions_from_tool_input(inp: dict) -> LBOAssumptions:
    return LBOAssumptions(
        entry_ev_multiple=inp["entry_ev_multiple"],
        exit_ev_multiple=inp["exit_ev_multiple"],
        hold_period_years=inp["hold_period_years"],
        leverage_multiple=inp["leverage_multiple"],
        revenue_growth_rate=inp["revenue_growth_rate"],
        da_pct_revenue=inp.get("da_pct_revenue", 0.03),
        capex_pct_revenue=inp.get("capex_pct_revenue", 0.03),
        nwc_pct_of_revenue_change=inp.get("nwc_pct_of_revenue_change", 0.15),
        tax_rate=inp.get("tax_rate", 0.25),
        transaction_fee_pct_of_ev=inp.get("transaction_fee_pct_of_ev", 0.02),
        financing_fee_pct_of_debt=inp.get("financing_fee_pct_of_debt", 0.02),
        cash_sweep_pct=inp.get("cash_sweep_pct", 0.8),
        management_rollover_pct=inp.get("management_rollover_pct", 0.0),
        tranches=[
            DebtTranche("Senior Term Loan", pct_of_total_debt=inp.get("senior_pct_of_debt", 0.75),
                        interest_rate=inp.get("senior_rate", 0.085),
                        mandatory_amort_pct_of_principal=inp.get("senior_mandatory_amort_pct", 0.01),
                        priority=1),
            DebtTranche("Subordinated Notes", pct_of_total_debt=1 - inp.get("senior_pct_of_debt", 0.75),
                        interest_rate=inp.get("sub_rate", 0.115),
                        mandatory_amort_pct_of_principal=0.0, priority=2),
        ],
    )


def execute_tool(name: str, tool_input: dict, state: AgentState, out_dir: Path) -> dict:
    if name == "get_company_financials":
        try:
            company = fetch_company_financials(tool_input["ticker"])
        except (ValueError, RuntimeError) as e:
            # bad ticker, or SEC unreachable after retries — report, don't crash the loop
            return {"error": str(e)}
        state.company = company
        latest = company.latest_complete_year()
        if latest:
            state.ltm_revenue, state.ltm_ebitda = latest.revenue, latest.ebitda
        return {
            "company_name": company.company_name,
            "cik": company.cik,
            "data_quality_flags": company.data_quality_flags,
            "years": [
                {"fy": y.fy, "revenue": y.revenue, "ebitda": y.ebitda,
                 "ebitda_source": y.ebitda_source, "total_debt": y.total_debt,
                 "cash": y.cash, "flags": y.flags}
                for y in company.years
            ],
        }

    if name == "verify_financials":
        if state.company is None:
            return {"error": "Call get_company_financials first."}
        year = state.company.latest_complete_year()
        if year is None:
            return {"error": "No complete fiscal year to verify."}
        try:
            result = verify_financials(
                state.company.cik, state.company.ticker, year.fy, year.revenue, year.ebitda,
                year.operating_income, year.d_and_a, year.net_income,
                year.interest_expense, year.income_tax_expense)
        except Exception as e:
            # Verification is a check, not a dependency — never let it kill the run.
            result = {"status": "unavailable",
                      "reason": f"{type(e).__name__}: {e}. Proceed, but say in the memo "
                                f"that the figures could not be cross-checked."}
        state.verification = result
        return result

    if name == "get_industry_benchmarks":
        try:
            result = get_industry_benchmarks(tool_input["industry"])
        except Exception as e:
            result = {"status": "unavailable",
                      "reason": f"{type(e).__name__}: {e}. Proceed without sector benchmarks."}
        if result.get("status") == "ok":
            state.benchmarks = result
        return result

    if name == "get_management_discussion":
        if state.company is None:
            return {"error": "Call get_company_financials first."}
        try:
            result = get_management_discussion(state.company.cik, tool_input.get("topics") or [])
        except Exception as e:
            result = {"status": "unavailable",
                      "reason": f"{type(e).__name__}: {e}. Write the narrative without "
                                f"management's own wording and say so."}
        if result.get("status") == "ok":
            state.mdna = result
        return result

    if name == "propose_and_run_lbo":
        if state.ltm_revenue is None or state.ltm_ebitda is None:
            return {"error": "No usable LTM financials yet — call get_company_financials first, "
                              "and check that a recent year has both revenue and EBITDA."}
        assumptions = _assumptions_from_tool_input(tool_input)
        try:
            result = run_lbo(state.ltm_revenue, state.ltm_ebitda, assumptions)
        except ValueError as e:
            # e.g. non-positive LTM EBITDA — this isn't a clean LBO candidate; tell Claude so
            return {"error": str(e),
                    "guidance": "This ticker is not a clean LBO candidate. Explain that to the "
                                "user and stop — do not force a model."}
        state.assumptions = assumptions
        state.result = result
        state.runs.append({"assumptions": assumptions, "result": result})
        return {
            "sources_uses": asdict(result.sources_uses),
            "exit_ebitda": result.exit_ebitda,
            "exit_enterprise_value": result.exit_enterprise_value,
            "exit_net_debt": result.exit_net_debt,
            "exit_equity_value": result.exit_equity_value,
            "sponsor_moic": result.sponsor_moic,
            "sponsor_irr": result.sponsor_irr,
            "warnings": result.warnings,
            "year_by_year": [
                {"year": p.year, "revenue": p.revenue, "ebitda": p.ebitda,
                 "levered_fcf_pre_sweep": p.levered_free_cash_flow_pre_sweep,
                 "total_debt_ending": p.total_debt_ending, "shortfall_flag": p.shortfall_flag}
                for p in result.projections
            ],
        }

    if name == "run_sensitivity":
        if state.assumptions is None:
            return {"error": "Call propose_and_run_lbo first."}
        base = base_run(state)
        grid = sensitivity_grid(state.ltm_revenue, state.ltm_ebitda,
                                 base["assumptions"] if base else state.assumptions,
                                 exit_multiples=tool_input["exit_multiples"],
                                 leverage_multiples=tool_input["leverage_multiples"])
        state.sensitivity = grid
        return grid

    if name == "write_excel_and_memo":
        if state.result is None or state.company is None:
            return {"error": "Need a completed LBO run (propose_and_run_lbo) before writing the file."}
        missing = [f for f in ("memo_text", "output_filename") if not tool_input.get(f)]
        if missing:
            return {"error": f"Missing required argument(s): {', '.join(missing)}.",
                    "guidance": "Call write_excel_and_memo again with the full memo text and "
                                "a filename like TICKER_LBO_Model.xlsx."}

        # Deterministic check that every dollar figure in the prose traces to a tool
        # result AND sits next to the right label. Blocks the write once so Claude can
        # correct itself; a second attempt writes anyway, with the findings attached so
        # a problem is never silently buried in the workbook.
        # The memo legitimately discusses the base case AND any upside case, so a label's
        # accepted values are the union across every run. Auditing against one run alone
        # would flag the other's correct figures.
        labelled: dict[str, list] = {}
        for run in (state.runs or [{"result": state.result}]):
            r = run["result"]
            for key, values in build_labelled_values(
                    state.company, state.ltm_revenue, state.ltm_ebitda,
                    r, r.sources_uses).items():
                labelled.setdefault(key, [])
                labelled[key].extend(v for v in values if v not in labelled[key])
        audit = audit_memo(tool_input["memo_text"], state.tool_values, labelled)
        state.memo_audit = audit
        state.memo_audit_attempts += 1
        if not audit["clean"] and state.memo_audit_attempts == 1:
            return {
                "error": "Memo figure audit failed — file NOT written.",
                "audit": audit,
                "guidance": "Fix each figure listed. 'mislabelled' means the number is "
                            "attached to the wrong quantity (e.g. citing EBITDA as cash) — "
                            "use the expected value shown. 'untraceable' means no tool "
                            "returned that number — correct it, or remove the claim. Then "
                            "call write_excel_and_memo again with the corrected memo.",
            }

        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / tool_input["output_filename"]
        # Build the workbook from the BASE case so the file matches the memo's headline.
        # Falls back to the last run only if the agent never ran a flat-multiple case.
        base = base_run(state) or {"assumptions": state.assumptions, "result": state.result}
        wb = build_workbook(state.company, state.ltm_revenue, state.ltm_ebitda,
                             base["assumptions"], base["result"],
                             sensitivity=state.sensitivity, memo_text=tool_input["memo_text"])
        save_workbook(wb, str(out_path))
        state.saved_to = str(out_path)
        state.memo_text = tool_input["memo_text"]
        return {"saved_to": str(out_path), "memo_audit": audit}

    return {"error": f"Unknown tool {name}"}


def _final_summary(state: AgentState) -> dict:
    """Serializable snapshot of the run for a UI to render (numbers only, no LLM prose math)."""
    if state.result is None:
        # Agent stopped without building a model (bad ticker / not a clean candidate).
        return {"completed": False,
                "company": state.company.company_name if state.company else None,
                "ticker": state.company.ticker if state.company else None,
                "data_quality_flags": state.company.data_quality_flags if state.company else [],
                "verification": state.verification}
    # Headline the BASE case (flat exit multiple), not whatever ran last. An upside case
    # run after the base case would otherwise become the dashboard's headline return.
    base = base_run(state)
    if base:
        r, a = base["result"], base["assumptions"]
    else:
        r, a = state.result, state.assumptions
    su = r.sources_uses
    return {
        "completed": True,
        "company": state.company.company_name,
        "ticker": state.company.ticker,
        "ltm_revenue": state.ltm_revenue,
        "ltm_ebitda": state.ltm_ebitda,
        "ltm_margin": (state.ltm_ebitda / state.ltm_revenue) if state.ltm_revenue else None,
        "assumptions": {
            "entry_ev_multiple": a.entry_ev_multiple,
            "exit_ev_multiple": a.exit_ev_multiple,
            "leverage_multiple": a.leverage_multiple,
            "hold_period_years": a.hold_period_years,
            "revenue_growth_rate": a.revenue_growth_rate,
            "cash_sweep_pct": a.cash_sweep_pct,
        },
        "entry_ev": su.purchase_enterprise_value,
        "sponsor_equity": su.sponsor_equity,
        "exit_equity_value": r.exit_equity_value,
        "moic": r.sponsor_moic,
        "irr": r.sponsor_irr,
        "sources_uses": asdict(su),
        "exit_ebitda": r.exit_ebitda,
        "exit_enterprise_value": r.exit_enterprise_value,
        "exit_net_debt": r.exit_net_debt,
        # Year-by-year series so a UI can chart the deleveraging without recomputing
        # anything itself — these are the engine's own numbers, passed straight through.
        "projections": [
            {"year": yr.year, "revenue": yr.revenue, "ebitda": yr.ebitda,
             "levered_fcf_pre_sweep": yr.levered_free_cash_flow_pre_sweep,
             "cash_swept": yr.cash_swept, "cash_balance_ending": yr.cash_balance_ending,
             "total_debt_ending": yr.total_debt_ending, "shortfall_flag": yr.shortfall_flag}
            for yr in r.projections
        ],
        "sensitivity": state.sensitivity,
        "verification": state.verification,
        "memo_audit": state.memo_audit,
        # Which case the headline figures above describe, and any expanded-multiple run
        # kept explicitly separate so a UI can never present it as the base case.
        "case": "base_flat" if base else "no_flat_case_run",
        "upside_cases": [
            {"entry_ev_multiple": u["assumptions"].entry_ev_multiple,
             "exit_ev_multiple": u["assumptions"].exit_ev_multiple,
             "moic": u["result"].sponsor_moic,
             "irr": u["result"].sponsor_irr,
             "exit_equity_value": u["result"].exit_equity_value}
            for u in upside_runs(state)
        ],
        "benchmarks": state.benchmarks,
        "management_discussion": state.mdna,
        "warnings": r.warnings,
        "memo_text": state.memo_text,
        "download": Path(state.saved_to).name if state.saved_to else None,
    }


def iter_agent_events(ticker: str, out_dir: Path = Path("output")):
    """
    Run the tool-use loop and YIELD structured events as they happen, instead of
    printing. This is the single source of truth for both the CLI (run_agent, below)
    and the web UI (webapp/server.py streams these events over SSE). Every event is a
    JSON-serializable dict with a "type" field.
    """
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from the environment
    state = AgentState()
    messages = [{"role": "user", "content": f"Build a quick LBO model for {ticker}."}]

    total_input_tokens = 0
    total_output_tokens = 0
    total_cache_write = 0
    total_cache_read = 0
    hit_cap = True

    for turn in range(MAX_TURNS):
        # Prompt caching. This loop re-sends the whole conversation every turn, so by
        # the last turn the same system prompt, tool schemas and early tool results have
        # been paid for ~9 times — re-sending, not the payloads, is what a run costs.
        #
        # Top-level cache_control auto-places the breakpoint on the last cacheable block,
        # which here is the newest tool result. Each turn therefore reads the previous
        # turn's prefix at ~0.1x and writes only the delta at 1.25x.
        #
        # Note the model matters: Haiku 4.5 needs a 4096-token prefix before anything
        # caches at all (the highest minimum of any current model — it is NOT monotonic
        # across generations). System + tools is only ~3K, so marking those alone would
        # silently cache nothing. Placing the breakpoint in the messages clears the
        # minimum from roughly the third turn on, which is where the tokens actually are.
        # 5-minute TTL is right: turns are seconds apart, and a read refreshes the timer.
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
            cache_control={"type": "ephemeral"},
        )
        total_input_tokens += response.usage.input_tokens
        total_output_tokens += response.usage.output_tokens
        total_cache_write += getattr(response.usage, "cache_creation_input_tokens", 0) or 0
        total_cache_read += getattr(response.usage, "cache_read_input_tokens", 0) or 0

        messages.append({"role": "assistant", "content": response.content})

        for b in response.content:
            if b.type == "text" and b.text.strip():
                yield {"type": "assistant", "text": b.text}

        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if not tool_uses:
            hit_cap = False
            break  # Claude gave a final answer with no further tool calls

        tool_results = []
        for tu in tool_uses:
            yield {"type": "tool_call", "name": tu.name, "input": tu.input}
            try:
                result = execute_tool(tu.name, tu.input, state, out_dir)
            except Exception as e:
                # Hand the failure back as a tool result so Claude can correct itself.
                # Raising here would abandon a run that has already been paid for.
                result = {"error": f"{type(e).__name__}: {e}",
                          "guidance": "That tool call failed. Check the arguments against "
                                      "the tool schema and try again."}
            # Every number any tool returned becomes the traceable set the memo audit
            # checks against. Collected here so it covers all tools automatically.
            if tu.name != "write_excel_and_memo":
                collect_values(result, state.tool_values)
            yield {"type": "tool_result", "name": tu.name, "result": result}
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tu.id,
                "content": json.dumps(result, default=str),
            })
        messages.append({"role": "user", "content": tool_results})

    if hit_cap:
        yield {"type": "cap", "max_turns": MAX_TURNS}
    yield {"type": "usage", "input_tokens": total_input_tokens,
           "output_tokens": total_output_tokens, "model": MODEL,
           "cache_write_tokens": total_cache_write, "cache_read_tokens": total_cache_read}
    yield {"type": "done", "summary": _final_summary(state)}


def run_agent(ticker: str, out_dir: Path = Path("output")) -> None:
    """CLI entry point — consumes iter_agent_events and prints it (behavior unchanged)."""
    for ev in iter_agent_events(ticker, out_dir):
        t = ev["type"]
        if t == "assistant":
            print(ev["text"])
        elif t == "tool_call":
            print(f"  [tool call] {ev['name']}({json.dumps(ev['input'])[:200]})")
        elif t == "cap":
            print(f"\n[stopped after hitting the {ev['max_turns']}-turn cap — see LBO_AGENT_MAX_TURNS]")
        elif t == "usage":
            print(f"\n--- usage this run: {ev['input_tokens']} input tokens, "
                  f"{ev['output_tokens']} output tokens ---")
            read, write = ev.get("cache_read_tokens", 0), ev.get("cache_write_tokens", 0)
            if read or write:
                # Cache reads bill at ~0.1x input, so this is most of the saving.
                billed = ev["input_tokens"] + write * 1.25 + read * 0.1
                naive = ev["input_tokens"] + write + read
                print(f"    prompt cache: {read:,} read + {write:,} written — "
                      f"billed like {billed:,.0f} input tokens instead of {naive:,.0f} "
                      f"({100 * (1 - billed / naive):.0f}% less)" if naive else "")
            print("Check current per-token pricing for your configured model at "
                  "https://docs.claude.com/en/docs/about-claude/models and set a spend "
                  "limit in the Anthropic Console so a run can never surprise-bill you.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="PaperLBO — autonomous LBO agent for a public company.")
    parser.add_argument("--ticker", required=True, help="Stock ticker, e.g. SHOE")
    parser.add_argument("--out-dir", default="output", help="Where to write the .xlsx file")
    args = parser.parse_args()
    run_agent(args.ticker, Path(args.out_dir))
