"""
Offline tests for the verification / benchmark / filing-parsing helpers.

Same philosophy as the other test modules: no network, no API key. Everything
tested here is a pure function, so the parts that decide what the memo must
disclose are checked deterministically.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from benchmarks import _token_score, match_industry  # noqa: E402
from filings import _find_topic, _strip_html, extract_mdna  # noqa: E402
from verify import _label_note, _pct_delta  # noqa: E402

INDUSTRIES = ["Apparel", "Retail (Special Lines)", "Retail (Grocery and Food)",
              "Retail (REITs)", "R.E.I.T.", "Shoe", "Restaurant/Dining", "Trucking",
              "Financial Svcs. (Non-bank & Insurance)"]


# ---- fiscal-year labelling — the check that catches the audited bug ----------

def test_january_year_end_is_flagged_ambiguous():
    """SHOE's real case: fy=2025 covers 2025-02-02..2026-01-31."""
    note, ambiguous = _label_note("2025-02-02", "2026-01-31", 2025)
    assert ambiguous is True
    assert "2026-01-31" in note
    assert "FY2026" in note          # tells the agent what a provider would call it


def test_december_year_end_is_not_flagged():
    note, ambiguous = _label_note("2025-01-01", "2025-12-31", 2025)
    assert ambiguous is False
    assert "unambiguous" in note


def test_the_specific_shoe_discrepancy_is_explained():
    """
    The audit compared $1.33B (SEC FY2021) against ~$977M (a provider's 'FY2021').
    Those are different periods; the note must say the provider's label is the prior one.
    """
    note, ambiguous = _label_note("2021-01-31", "2022-01-29", 2021)
    assert ambiguous is True
    assert "prior period" in note


# ---- discrepancy threshold ---------------------------------------------------

def test_pct_delta_is_symmetric_and_scaled():
    assert _pct_delta(100.0, 100.0) == 0.0
    assert _pct_delta(100.0, 90.0) == _pct_delta(90.0, 100.0)
    assert abs(_pct_delta(1_330_394_000, 977_000_000) - 0.2656) < 0.001   # well over 5%
    assert _pct_delta(0, 0) is None


def test_ebitda_two_path_gap_below_threshold():
    """SHOE's real figures: 3.8% apart, so reported but not flagged as a discrepancy."""
    assert _pct_delta(101_106_000, 105_108_000) < 0.05


# ---- industry matching -------------------------------------------------------

def test_matches_exact_and_loose_industry_names():
    assert match_industry("Retail (Special Lines)", INDUSTRIES) == "Retail (Special Lines)"
    assert match_industry("specialty retail", INDUSTRIES) == "Retail (Special Lines)"
    assert match_industry("restaurants", INDUSTRIES) == "Restaurant/Dining"


def test_short_abbreviated_names_do_not_swallow_queries():
    """'R.E.I.T.' tokenizes to single letters; prefix matching must not match everything."""
    assert _token_score("trucking company", "R.E.I.T.") == 0.0
    assert match_industry("trucking", INDUSTRIES) == "Trucking"


def test_nonsense_returns_no_match():
    assert match_industry("zzz nonsense", INDUSTRIES) is None


# ---- MD&A extraction ---------------------------------------------------------

FAKE_10K = """
    TABLE OF CONTENTS
    Item 7. Management's Discussion and Analysis of Financial Condition
    Item 8. Financial Statements and Supplementary Data
    PART II
    Item 7. Management's Discussion and Analysis of Financial Condition and Results
    Gross profit margin increased 100 basis points driven by disciplined pricing.
    """ + ("Filler narrative about operations. " * 40) + """
    Item 7A. Quantitative and Qualitative Disclosures About Market Risk
    Not applicable.
    Item 8. Financial Statements and Supplementary Data
    """


def test_extract_mdna_skips_the_table_of_contents():
    section = extract_mdna(FAKE_10K)
    assert section is not None
    assert "disciplined pricing" in section          # got the real body...
    assert "Quantitative" not in section             # ...and stopped at Item 7A


def test_extract_mdna_returns_none_when_absent():
    assert extract_mdna("Item 1. Business. Nothing else here.") is None


def test_strip_html_drops_markup_and_entities():
    out = _strip_html("<p>Gross&#160;margin <b>36.6%</b></p><table><tr><td>9</td></tr></table>")
    assert "36.6%" in out
    assert "<" not in out and "&#160;" not in out
    assert "9" not in out          # tables are dropped, not flattened into the prose


# ---- regressions from real filings ------------------------------------------

def test_curly_apostrophe_heading_is_found():
    """
    Filings write MANAGEMENT'S with a typographic apostrophe. Folding it to ASCII in
    _strip_html is what lets the heading regex match; this pins that behaviour.
    """
    raw = "<p>ITEM 7.\u2019 filler</p><p>ITEM 7. MANAGEMENT\u2019S DISCUSSION AND ANALYSIS</p>" \
          + "<p>body text here</p>" * 20 + "<p>ITEM 7A. QUANTITATIVE</p>"
    text = _strip_html(raw)
    assert "\u2019" not in text
    assert extract_mdna(text) is not None


def test_heading_with_quote_after_the_period():
    """1-800-Flowers writes: Item 7. \"Management's Discussion and Analysis...\""""
    text = ('Item 7. "Management\'s Discussion and Analysis of Financial Condition'
            + " body " * 200 + "Item 7A. Quantitative")
    assert extract_mdna(text) is not None


def test_cross_references_are_not_mistaken_for_the_heading():
    """
    'ITEM 7, "Management's...' (comma) and 'ITEM 8 of this Annual Report' (no
    delimiter) are prose cross-references, not section headings.
    """
    text = ('See ITEM 7, "Management\'s Discussion and Analysis" and ITEM 8 of this '
            'Annual Report for more information. ' * 5)
    assert extract_mdna(text) is None


def test_find_topic_falls_back_from_exact_phrase():
    mdna = ("Net Sales grew. Our comparable stores Net Sales declined high-single digits "
            "in the period, which management attributes to traffic.")
    assert _find_topic(mdna, "comparable stores Net Sales") is not None   # exact
    assert _find_topic(mdna, "comparable store sales") is not None        # words-in-window
    assert _find_topic(mdna, "quantum entanglement") is None              # genuinely absent


# ---- memo figure audit -------------------------------------------------------

from memo_audit import (audit_memo, collect_values,  # noqa: E402
                        extract_currency_figures, _parse_figure)

# A minimal stand-in for one run's tool output.
TOOL_VALUES = collect_values({
    "ltm_revenue": 1_135_324_000, "ltm_ebitda": 101_106_000, "cash": 117_091_000,
    "fy2021": {"revenue": 1_330_394_000, "ebitda": 226_406_000},
    "sources_uses": {"sponsor_equity": 293_000_000, "total_new_debt": 252_765_000},
    "mdna": "capital expenditures in Fiscal 2026 of between $5 to $7 million",
})
LABELLED = {
    "cash": [117_091_000],
    "revenue": [1_135_324_000, 1_330_394_000],
    "ebitda": [101_106_000, 226_406_000],
    "debt": [0.0, 252_765_000],
    "sponsor equity": [293_000_000],
}


def test_audit_catches_ebitda_cited_as_cash():
    """
    The exact audited bug. $101M IS a real tool value (EBITDA), so a pure
    traceability check passes it — only the label check catches it.
    """
    memo = "Net cash position of ~$117M ($101M cash - $0M debt)"
    result = audit_memo(memo, TOOL_VALUES, LABELLED)
    assert result["clean"] is False
    flagged = [(m["figure"], m["labelled_as"]) for m in result["mislabelled"]]
    assert ("$101M", "cash") in flagged
    assert not any(f["figure"] == "$101M" for f in result["untraceable"])   # it IS traceable


def test_audit_catches_invented_figure():
    result = audit_memo("Net cash position of ~$9M.", TOOL_VALUES, LABELLED)
    assert [u["figure"] for u in result["untraceable"]] == ["$9M"]


def test_correct_memo_passes_clean():
    memo = ("Revenue: $1,135M\n"
            "EBITDA: $101M\n"
            "Net cash position of ~$117M ($117M cash - $0M debt)\n"
            "Revenue declined from $1.33B in FY2021.\n"
            "Sponsor equity invested: $293.0M\n")
    result = audit_memo(memo, TOOL_VALUES, LABELLED)
    assert result["clean"] is True, result["summary"]


def test_nearest_label_wins_by_edge_distance():
    """'$0M debt' must bind to 'debt' even though 'cash' precedes it on the same line."""
    result = audit_memo("Net cash position of ~$117M ($117M cash - $0M debt)",
                        TOOL_VALUES, LABELLED)
    assert result["clean"] is True


def test_labels_do_not_leak_across_lines():
    """A label on one line must not describe a figure on the next."""
    result = audit_memo("Cash: $117M\nEBITDA: $101M", TOOL_VALUES, LABELLED)
    assert result["clean"] is True


def test_tolerance_follows_written_precision():
    value, tol = _parse_figure("1.1", "B")
    assert abs(value - 1.1e9) < 1 and tol == 5e7      # rounded citation allowed
    assert abs(1_135_324_000 - value) <= tol
    value, tol = _parse_figure("101", "M")
    assert tol == 5e5
    assert abs(101_106_000 - value) <= tol            # $101M covers $101.106M
    assert abs(117_091_000 - value) > tol             # but not $117M


def test_quoted_mdna_figures_are_traceable():
    """Management quotes carry dollar figures; repeating them must not be flagged."""
    memo = "Management expects capex of $5 to $7 million in Fiscal 2026."
    assert not audit_memo(memo, TOOL_VALUES, LABELLED)["untraceable"]


def test_extracts_scale_suffixes_and_plain_numbers():
    figs = {f["literal"]: f["value"] for f in
            extract_currency_figures("$1.33B and $505.5M and $117,091,000 and $0M")}
    assert figs["$1.33B"] == 1.33e9
    assert figs["$505.5M"] == 505_500_000
    assert figs["$117,091,000"] == 117_091_000
    assert figs["$0M"] == 0.0


def test_generic_sector_query_is_ambiguous_not_guessed():
    """
    'retail' sits inside eight industries. Returning the shortest silently produced
    'Retail (REITs)' as the benchmark for a footwear retailer in a real run.
    """
    from benchmarks import resolve_industry
    match, candidates = resolve_industry("retail", INDUSTRIES)
    assert match is None
    assert "Retail (Special Lines)" in candidates and "Retail (REITs)" in candidates
    assert resolve_industry("specialty retail", INDUSTRIES)[0] == "Retail (Special Lines)"


# ---- base-case selection -----------------------------------------------------

import agent as agent_mod  # noqa: E402
from lbo_engine import run_lbo as _run_lbo  # noqa: E402

LTM_REV, LTM_EBITDA = 1_135_324_000, 101_106_000


def _add_run(state, entry, exit_):
    a = agent_mod._assumptions_from_tool_input(
        {"entry_ev_multiple": entry, "exit_ev_multiple": exit_, "hold_period_years": 5,
         "leverage_multiple": 3.5, "revenue_growth_rate": 0.0})
    r = _run_lbo(LTM_REV, LTM_EBITDA, a)
    state.runs.append({"assumptions": a, "result": r})
    state.assumptions, state.result = a, r      # mirrors execute_tool
    return a, r


def _state():
    s = agent_mod.AgentState()
    s.ltm_revenue, s.ltm_ebitda = LTM_REV, LTM_EBITDA
    return s


def test_upside_run_after_base_does_not_become_the_headline():
    """
    The regression this guards: the agent runs a flat base case, then an upside case
    with an expanded exit multiple. The last run used to drive the dashboard headline,
    the workbook, and the sensitivity grid.
    """
    s = _state()
    _add_run(s, 6.0, 6.0)
    _add_run(s, 6.0, 7.0)                       # upside, run LAST
    assert s.assumptions.exit_ev_multiple == 7.0             # latest really is the upside
    assert agent_mod.base_run(s)["assumptions"].exit_ev_multiple == 6.0
    assert len(agent_mod.upside_runs(s)) == 1


def test_base_case_is_the_most_recent_flat_run():
    """A revised base case supersedes the first attempt."""
    s = _state()
    _add_run(s, 7.0, 7.0)
    _add_run(s, 5.5, 5.5)
    assert agent_mod.base_run(s)["assumptions"].entry_ev_multiple == 5.5
    assert agent_mod.upside_runs(s) == []


def test_no_flat_run_is_reported_rather_than_faked():
    """If the agent only ever ran an expanded-multiple case, say so — don't relabel it."""
    s = _state()
    _add_run(s, 5.0, 7.0)
    assert agent_mod.base_run(s) is None
    assert len(agent_mod.upside_runs(s)) == 1


def test_contracting_exit_multiple_is_not_an_upside():
    """Exit below entry is a downside case; it is neither base nor upside."""
    s = _state()
    _add_run(s, 7.0, 6.0)
    assert agent_mod.base_run(s) is None
    assert agent_mod.upside_runs(s) == []


# ---- revision budget ---------------------------------------------------------

def _propose(state, entry, exit_):
    return agent_mod.execute_tool("propose_and_run_lbo",
        {"entry_ev_multiple": entry, "exit_ev_multiple": exit_, "hold_period_years": 5,
         "leverage_multiple": 3.0, "revenue_growth_rate": 0.0}, state, None)


def _priced_state():
    from edgar import CompanyFinancials, FiscalYearFinancials
    s = agent_mod.AgentState()
    s.ltm_revenue, s.ltm_ebitda = LTM_REV, LTM_EBITDA
    s.company = CompanyFinancials(ticker="SHOE", cik="1", company_name="SHOE",
        years=[FiscalYearFinancials(fy=2025, revenue=LTM_REV, ebitda=LTM_EBITDA)])
    return s


def test_base_case_allows_exactly_one_revision():
    """
    The prompt said 'rerun ONCE' and a real run called the engine four times. Each
    extra cycle is output tokens at 5x the input rate, so the cap is enforced.
    """
    s = _priced_state()
    assert "error" not in _propose(s, 6.5, 6.5)      # initial
    assert "error" not in _propose(s, 5.5, 5.5)      # the one allowed revision
    refused = _propose(s, 5.0, 5.0)                  # a third base case
    assert "error" in refused and "budget spent" in refused["error"]
    assert len(s.runs) == 2                          # the refused run was never recorded


def test_one_upside_run_is_still_allowed_after_the_base_budget():
    s = _priced_state()
    _propose(s, 6.5, 6.5); _propose(s, 5.5, 5.5)
    assert "error" not in _propose(s, 5.5, 7.0)      # the labelled upside case
    assert "error" in _propose(s, 5.0, 8.0)          # but only one
    assert len(agent_mod.upside_runs(s)) == 1


def test_budget_refusal_leaves_the_run_usable():
    """A refusal must not strand the agent — the prior result still writes a workbook."""
    s = _priced_state()
    _propose(s, 6.5, 6.5); _propose(s, 5.5, 5.5); _propose(s, 5.0, 5.0)
    assert s.result is not None
    assert agent_mod.base_run(s)["assumptions"].entry_ev_multiple == 5.5


def test_refusal_tells_the_agent_what_to_do_instead():
    s = _priced_state()
    _propose(s, 6.5, 6.5); _propose(s, 5.5, 5.5)
    refused = _propose(s, 5.0, 5.0)
    assert "legitimate finding" in refused["guidance"]
