"""
verify.py — independent cross-checks on the EDGAR-derived figures, before the
memo gets written.

WHY THIS EXISTS
---------------
An audit of a generated SHOE workbook flagged that the memo cited FY2021 revenue
of $1.33B while a third-party source showed ~$977M. The EDGAR pull was correct and
the memo reported it faithfully — the two numbers are different *periods*:

    SEC labels a fiscal year by the year it STARTS.
      fy=2021  ->  2021-01-31 .. 2022-01-29   ->  $1.33B
    Many data providers label it by the year it ENDS.
      "FY2021" ->  2020-02-02 .. 2021-01-30   ->  ~$977M  (the COVID year)

For any filer with a non-December year-end (all of retail), those two conventions
disagree by a full year, and nothing in the pipeline said which one was in use.
That is the class of error this module exists to surface: not bad arithmetic, but
an unlabeled period.

WHAT IT CHECKS (all free, no API key, SEC data only)
----------------------------------------------------
1. Period alignment  — the actual start/end dates behind the fiscal-year label,
   plus an explicit warning when the label is ambiguous across conventions.
2. Revenue, cross-tag — other XBRL revenue tags covering the SAME period end.
3. EBITDA, two paths — operating income + D&A vs. the net income buildup.
4. Restatement       — the value as first filed vs. as most recently filed, which
   is a genuinely independent observation (different filings, different dates).

WHAT IT CANNOT CATCH
--------------------
These are all SEC-sourced, so they cannot detect an error in SEC's own data. A
true second opinion needs a third-party provider; `_third_party_check` is the seam
for that and currently reports "not configured" by design.

Everything here is best-effort: any failure returns a status the agent can report
and proceed from, never an exception that kills the run.
"""

from __future__ import annotations

from typing import Optional

from edgar import COMPANYCONCEPT_URL, TAG_CANDIDATES, _get_json

# Two sources are treated as disagreeing past this much relative difference.
MATERIAL_DELTA = 0.05


def _pct_delta(a: float, b: float) -> Optional[float]:
    """Relative difference between two figures, scaled by the larger magnitude."""
    denom = max(abs(a), abs(b))
    if not denom:
        return None
    return abs(a - b) / denom


def _annual_entries(cik10: str, tag: str) -> list[dict]:
    """Every 10-K annual entry for one tag — NOT deduped, so restatements survive."""
    data = _get_json(COMPANYCONCEPT_URL.format(cik10=cik10, tag=tag))
    if not data:
        return []
    rows = data.get("units", {}).get("USD", [])
    return [e for e in rows
            if e.get("form") in ("10-K", "10-K/A") and e.get("fp") == "FY" and e.get("end")]


def _period_for_fy(cik10: str, fy: int) -> tuple[Optional[dict], Optional[str]]:
    """
    Find the entry that defines fiscal year `fy`: the one SEC labels fy, taking the
    latest period end (a 10-K tags its comparative years with the filing's own fy).
    Returns (entry, tag_name).
    """
    best, best_tag = None, None
    for tag in TAG_CANDIDATES["revenue"]:
        for e in _annual_entries(cik10, tag):
            if e.get("fy") != fy:
                continue
            key = (e.get("filed", ""), e["end"])
            if best is None or key > (best.get("filed", ""), best["end"]):
                best, best_tag = e, tag
    return best, best_tag


def _label_note(period_start: str, period_end: str, fy: int) -> tuple[str, bool]:
    """
    Explain which fiscal-year convention this period sits in, and whether the label
    is ambiguous. December year-ends are unambiguous; everything else is not.
    """
    end_year = int(period_end[:4])
    end_month = int(period_end[5:7])
    if end_month == 12:
        return (f"FY{fy} covers {period_start} to {period_end}. December year-end, so "
                f"the label is unambiguous across data providers."), False
    return (
        f"FY{fy} covers {period_start} to {period_end}. SEC labels a fiscal year by the "
        f"year it STARTS; many third-party providers label by the year it ENDS and would "
        f"call this same period FY{end_year}. Their 'FY{fy}' is most likely the prior "
        f"period (roughly {fy - 1}-02 to {fy}-01), a DIFFERENT year of results. State the "
        f"period end date in the memo whenever you cite a fiscal year for this company."
    ), True


def _third_party_check(ticker: str) -> dict:
    """
    Seam for a genuine second opinion (a non-SEC data vendor).

    Deliberately not wired up: every free option evaluated (Financial Modeling Prep,
    Alpha Vantage) requires an account and an API key, and the project owner opted to
    skip both for now. When one is added, return the same shape as the checks above —
    {"status": "ok", "revenue": ..., "ebitda": ..., "delta_pct": ...} — and the agent
    prompt needs no change.
    """
    return {
        "status": "not_configured",
        "note": "No third-party data source is configured, so every check below is "
                "SEC-sourced and cannot detect an error in SEC's own filings data.",
    }


def verify_financials(cik10: str, ticker: str, fy: int, revenue: Optional[float],
                      ebitda: Optional[float], operating_income: Optional[float],
                      d_and_a: Optional[float], net_income: Optional[float],
                      interest_expense: Optional[float],
                      income_tax_expense: Optional[float]) -> dict:
    """
    Cross-check the LTM figures the model is about to be built on. Best-effort:
    on any failure, returns status "unavailable" with a reason rather than raising.
    """
    out: dict = {
        "fy": fy,
        "checks_run": [],
        "discrepancies": [],
        "third_party": _third_party_check(ticker),
    }

    try:
        entry, tag = _period_for_fy(cik10, fy)
    except Exception as e:
        return {**out, "status": "unavailable",
                "reason": f"Could not reach SEC to verify ({type(e).__name__}: {e}). "
                          "Proceed, but say in the memo that the figures are unverified."}

    if entry is None:
        return {**out, "status": "unavailable",
                "reason": f"No 10-K revenue entry found for FY{fy} to anchor the period dates."}

    # ---- 1. period alignment -------------------------------------------------
    start, end = entry.get("start", ""), entry["end"]
    note, ambiguous = _label_note(start, end, fy)
    out["period"] = {"fiscal_year_label": f"FY{fy}", "period_start": start, "period_end": end,
                     "filed": entry.get("filed"), "source_tag": tag,
                     "label_ambiguous_across_providers": ambiguous, "note": note}
    out["checks_run"].append("period_alignment")
    if ambiguous:
        out["discrepancies"].append({
            "check": "fiscal_year_label",
            "severity": "explain_in_memo",
            "detail": note,
        })

    # ---- 2. revenue, across alternate XBRL tags ------------------------------
    alternates = []
    for alt_tag in TAG_CANDIDATES["revenue"]:
        if alt_tag == tag:
            continue
        try:
            match = [e for e in _annual_entries(cik10, alt_tag) if e["end"] == end]
        except Exception:
            continue
        if not match:
            continue
        val = sorted(match, key=lambda e: e.get("filed", ""))[-1]["val"]
        delta = _pct_delta(revenue, val) if revenue else None
        alternates.append({"tag": alt_tag, "value": val, "delta_pct": delta})
        if delta is not None and delta > MATERIAL_DELTA:
            out["discrepancies"].append({
                "check": "revenue_cross_tag", "severity": "explain_in_memo",
                "detail": f"For the period ending {end}, tag {tag} reports "
                          f"${revenue:,.0f} but {alt_tag} reports ${val:,.0f} "
                          f"({delta:.1%} apart). These tags legitimately differ when one "
                          f"includes sales tax collected and the other excludes it — say "
                          f"which one the model uses.",
                "values": {tag: revenue, alt_tag: val},
            })
    out["revenue"] = {"model_uses": revenue, "source_tag": tag, "alternate_tags": alternates}
    out["checks_run"].append("revenue_cross_tag")

    # ---- 3. EBITDA, two independent buildups ---------------------------------
    op_path = (operating_income + d_and_a) if None not in (operating_income, d_and_a) else None
    buildup = None
    if None not in (net_income, interest_expense, income_tax_expense, d_and_a):
        buildup = net_income + interest_expense + income_tax_expense + d_and_a
    delta = _pct_delta(op_path, buildup) if None not in (op_path, buildup) else None
    out["ebitda"] = {"model_uses": ebitda, "operating_income_plus_da": op_path,
                     "net_income_buildup": buildup, "delta_pct": delta}
    out["checks_run"].append("ebitda_two_paths")
    if delta is not None and delta > MATERIAL_DELTA:
        out["discrepancies"].append({
            "check": "ebitda_two_paths", "severity": "explain_in_memo",
            "detail": f"EBITDA is ${op_path:,.0f} via operating income + D&A but "
                      f"${buildup:,.0f} via the net income buildup ({delta:.1%} apart). "
                      f"The gap is non-operating items (other income/expense, impairments) "
                      f"sitting below operating income. The model uses ${ebitda:,.0f}.",
            "values": {"operating_income_plus_da": op_path, "net_income_buildup": buildup},
        })

    # ---- 4. restatement: as first filed vs. as most recently filed -----------
    try:
        same_period = [e for e in _annual_entries(cik10, tag) if e["end"] == end]
        by_filed = sorted(same_period, key=lambda e: e.get("filed", ""))
        if len(by_filed) >= 2:
            first, latest = by_filed[0], by_filed[-1]
            delta = _pct_delta(first["val"], latest["val"])
            out["restatement"] = {
                "as_first_filed": {"value": first["val"], "filed": first.get("filed")},
                "as_last_filed": {"value": latest["val"], "filed": latest.get("filed")},
                "delta_pct": delta, "filings_compared": len(by_filed),
            }
            out["checks_run"].append("restatement")
            if delta is not None and delta > MATERIAL_DELTA:
                out["discrepancies"].append({
                    "check": "restatement", "severity": "explain_in_memo",
                    "detail": f"Revenue for the period ending {end} was first reported as "
                              f"${first['val']:,.0f} (filed {first.get('filed')}) but appears "
                              f"as ${latest['val']:,.0f} in a later filing "
                              f"(filed {latest.get('filed')}) — {delta:.1%} apart. The figure "
                              f"was restated; the model uses the latest.",
                    "values": {"first_filed": first["val"], "last_filed": latest["val"]},
                })
    except Exception:
        pass  # best-effort — a missing restatement check is not worth failing the run

    out["status"] = "ok"
    out["agreed"] = not out["discrepancies"]
    out["summary"] = (
        "All cross-checks agree; no discrepancy to report."
        if out["agreed"] else
        f"{len(out['discrepancies'])} item(s) need to be stated explicitly in the memo."
    )
    return out
