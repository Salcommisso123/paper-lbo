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

from edgar import CompanyFinancials, fetch_company_financials  # noqa: E402
from excel_writer import build_workbook, save_workbook  # noqa: E402
from lbo_engine import DebtTranche, LBOAssumptions, run_lbo, sensitivity_grid  # noqa: E402

# Default to Haiku 4.5 — the cheapest current model, and it produces a solid memo
# (~3.5 cents/run at typical token volumes). Bump to a stronger model via LBO_AGENT_MODEL
# for the polished version you send to a recruiter. See docs/COST_CONTROL.md.
MODEL = os.environ.get("LBO_AGENT_MODEL", "claude-haiku-4-5")
MAX_TOKENS = int(os.environ.get("LBO_AGENT_MAX_TOKENS", "3000"))
MAX_TURNS = int(os.environ.get("LBO_AGENT_MAX_TURNS", "8"))  # hard stop so a confused loop can't burn budget

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
3. Propose deal assumptions using standard lower-middle/mid-market heuristics, and \
briefly justify each choice by referencing the company's actual revenue size, margin, \
and growth from step 1:
   - Entry EV/EBITDA multiple: roughly 6-9x for smaller, less differentiated businesses; \
higher for larger, more stable, higher-margin ones.
   - Leverage: roughly 3-4x EBITDA for smaller/cyclical/lower-margin businesses, \
4.5-5.5x for larger/stable/higher-margin ones. Never propose leverage that implies \
Year-1 interest expense anywhere near EBITDA.
   - Exit multiple: default to the SAME as entry (no assumed multiple expansion) unless \
you have a specific, stated reason to assume otherwise — this is the conservative, \
defensible convention.
   - Hold period: 5 years unless there's a reason to pick something else.
   - Leave operating assumptions (D&A %, capex %, NWC %, tax rate, fees) at the tool's \
sensible defaults unless the company's data suggests otherwise.
   - Cash sweep: keep it around the 0.8 (80%) default. A 100% sweep overstates returns by \
assuming every dollar of free cash flow pays down debt with no cash cushion retained; ~80% \
is the more defensible convention.
4. Call propose_and_run_lbo with those assumptions.
5. Sanity-check the result. If there are warnings (cash flow shortfall, negative equity) \
or the IRR is outside a believable ~10-40% range, adjust ONE assumption (usually leverage \
or entry multiple) and rerun ONCE. Don't iterate more than that — explain the tension \
instead of hiding it.
6. Call run_sensitivity to build an IRR/MOIC grid across exit multiple and leverage.
7. Write a short investment memo (structured like: situation, why it clears a screen, \
base-case returns, key sensitivities, what you'd want to verify before this goes further) \
using ONLY numbers that came back from the tools. Do not invent or round-trip numbers \
from memory.
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
        self.sensitivity: dict | None = None


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
        grid = sensitivity_grid(state.ltm_revenue, state.ltm_ebitda, state.assumptions,
                                 exit_multiples=tool_input["exit_multiples"],
                                 leverage_multiples=tool_input["leverage_multiples"])
        state.sensitivity = grid
        return grid

    if name == "write_excel_and_memo":
        if state.result is None or state.company is None:
            return {"error": "Need a completed LBO run (propose_and_run_lbo) before writing the file."}
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / tool_input["output_filename"]
        wb = build_workbook(state.company, state.ltm_revenue, state.ltm_ebitda,
                             state.assumptions, state.result,
                             sensitivity=state.sensitivity, memo_text=tool_input["memo_text"])
        save_workbook(wb, str(out_path))
        return {"saved_to": str(out_path)}

    return {"error": f"Unknown tool {name}"}


def run_agent(ticker: str, out_dir: Path = Path("output")) -> None:
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from the environment
    state = AgentState()
    messages = [{"role": "user", "content": f"Build a quick LBO model for {ticker}."}]

    total_input_tokens = 0
    total_output_tokens = 0

    for turn in range(MAX_TURNS):
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )
        total_input_tokens += response.usage.input_tokens
        total_output_tokens += response.usage.output_tokens

        messages.append({"role": "assistant", "content": response.content})

        tool_uses = [b for b in response.content if b.type == "tool_use"]
        text_blocks = [b.text for b in response.content if b.type == "text"]
        if text_blocks:
            print("\n".join(text_blocks))

        if not tool_uses:
            break  # Claude gave a final answer with no further tool calls

        tool_results = []
        for tu in tool_uses:
            print(f"  [tool call] {tu.name}({json.dumps(tu.input)[:200]})")
            result = execute_tool(tu.name, tu.input, state, out_dir)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tu.id,
                "content": json.dumps(result, default=str),
            })
        messages.append({"role": "user", "content": tool_results})
    else:
        print(f"\n[stopped after hitting the {MAX_TURNS}-turn cap — see LBO_AGENT_MAX_TURNS]")

    print(f"\n--- usage this run: {total_input_tokens} input tokens, "
          f"{total_output_tokens} output tokens ---")
    print("Check current per-token pricing for your configured model at "
          "https://docs.claude.com/en/docs/about-claude/models and set a spend "
          "limit in the Anthropic Console so a run can never surprise-bill you.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="PaperLBO — autonomous LBO agent for a public company.")
    parser.add_argument("--ticker", required=True, help="Stock ticker, e.g. SHOE")
    parser.add_argument("--out-dir", default="output", help="Where to write the .xlsx file")
    args = parser.parse_args()
    run_agent(args.ticker, Path(args.out_dir))
