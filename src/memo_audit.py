"""
memo_audit.py — deterministic check that every dollar figure in the memo traces
back to a number a tool actually returned, and is attached to the right label.

WHY THIS EXISTS
---------------
A generated SHOE memo said:

    "Net cash position of ~$9M ($101M cash - $0M debt)"

$101M is the LTM EBITDA. Cash was $117M. Nothing in the pipeline could catch that:
the engine was right, the tool output was right, and the error appeared only in prose
the LLM wrote afterwards.

TWO CHECKS, AND WHY BOTH ARE NEEDED
-----------------------------------
1. TRACEABILITY — does this dollar figure appear anywhere in any tool result?
   Catches invented numbers ("~$9M" above, which matches nothing).

2. LABEL MATCH — is the figure next to the word "cash" actually the cash value?
   Catches the $101M error, which check 1 CANNOT: $101M is a real tool value, just
   the wrong one. A figure can be perfectly traceable and still be a lie about what
   it measures.

Check 2 is the one that fires on the audited bug. Check 1 is the broader net.

FALSE POSITIVES ARE HANDLED BY CONSTRUCTION, NOT BY LOOSENING
-------------------------------------------------------------
- Tolerance follows the WRITTEN precision: "$1.1B" is judged against half of its last
  significant digit, so a legitimately rounded citation of $1,135,324,000 passes while
  "$9M" against $117M does not.
- A label's accepted values include its whole time series (every filed year plus every
  projected year), so "exit EBITDA: $91.4M" is not judged against LTM EBITDA.
- Numbers quoted from MD&A text are part of the traceable set, so management quotes
  containing figures don't get flagged.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Optional

# Written-in-prose scale suffixes.
SCALES = {
    "": 1.0, "k": 1e3, "thousand": 1e3,
    "m": 1e6, "mm": 1e6, "million": 1e6,
    "b": 1e9, "bn": 1e9, "billion": 1e9,
}

CURRENCY_RE = re.compile(
    r"\$\s?(\d[\d,]*(?:\.\d+)?)\s*(billion|million|thousand|bn|mm|[bmk])?\b",
    re.IGNORECASE,
)

# How far from a figure we look for a label word.
LABEL_WINDOW = 60

# Qualifiers that mean a figure belongs to a projected/other period, not the LTM value.
# The label's full series is checked anyway; this just avoids pointless noise.
CONTEXT_RE = re.compile(r"(?i)\b(exit|year\s*\d|yr\s*\d|projected|forecast|terminal|"
                        r"fy\d{4}|prior|previous)\b")


def _parse_figure(literal: str, suffix: Optional[str]) -> tuple[float, float]:
    """
    Return (value, tolerance). Tolerance is half of the last written significant digit,
    so the check respects how precisely the author actually wrote the number:
        "$1.1B"    -> +/- 0.05B   (a rounded citation of $1.135B passes)
        "$101M"    -> +/- 0.5M    ($101.106M passes)
        "$9M"      -> +/- 0.5M    ($117M does not)
    """
    scale = SCALES[(suffix or "").lower()]
    clean = literal.replace(",", "")
    value = float(clean) * scale
    decimals = len(clean.split(".")[1]) if "." in clean else 0
    tolerance = 0.5 * (10 ** -decimals) * scale
    return value, tolerance


def extract_currency_figures(text: str) -> list[dict]:
    """
    Every $-denominated figure in the memo, with the prose around it.

    Context stops at line boundaries. A memo puts one fact per line, so letting the
    window run into neighbouring lines makes the label on one line appear to describe
    a figure on another — which reads every number in the block as 'cash'.
    """
    out = []
    for m in CURRENCY_RE.finditer(text):
        value, tol = _parse_figure(m.group(1), m.group(2))
        line_start = text.rfind("\n", 0, m.start()) + 1
        line_end = text.find("\n", m.end())
        line_end = len(text) if line_end == -1 else line_end
        start = max(line_start, m.start() - LABEL_WINDOW)
        end = min(line_end, m.end() + LABEL_WINDOW)
        out.append({"literal": m.group(0).strip(), "value": value, "tolerance": tol,
                    "context": " ".join(text[start:end].split()),
                    # span of the figure within `context`, for nearest-label distance
                    "span_in_context": (m.start() - start, m.end() - start)})
    return out


def collect_values(obj: Any, into: Optional[set] = None) -> set:
    """
    Every number reachable in a tool result, including numbers written inside strings
    (MD&A quotes carry figures like "$5 to $7 million" that the memo may repeat).
    """
    into = set() if into is None else into
    if isinstance(obj, bool) or obj is None:
        return into
    if isinstance(obj, (int, float)):
        into.add(float(obj))
    elif isinstance(obj, str):
        for m in CURRENCY_RE.finditer(obj):
            into.add(_parse_figure(m.group(1), m.group(2))[0])
    elif isinstance(obj, dict):
        for v in obj.values():
            collect_values(v, into)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            collect_values(v, into)
    return into


def _matches(value: float, tolerance: float, candidates: Iterable[float]) -> bool:
    return any(abs(value - c) <= tolerance for c in candidates if c is not None)


def audit_memo(memo_text: str, tool_values: set, labelled: dict[str, list]) -> dict:
    """
    Check the memo's dollar figures.

    `tool_values`  every number any tool returned this run.
    `labelled`     {label_word: [accepted values...]} — a label's whole series, so a
                   projected-year figure is not judged against the LTM value.

    Returns findings; never raises. `clean` is True only if nothing was flagged.
    """
    untraceable, mislabelled = [], []

    for fig in extract_currency_figures(memo_text):
        value, tol, ctx = fig["value"], fig["tolerance"], fig["context"]

        # --- check 2: is it attached to the right label? ----------------------
        # Use the NEAREST label to the figure. "$0M debt" sits on the same line as
        # "cash"; whichever word is closer is the one describing this number.
        fig_start, fig_end = fig["span_in_context"]
        nearest, best_gap = None, None
        for label, accepted in labelled.items():
            for lm in re.finditer(rf"(?i)\b{re.escape(label)}\b", ctx):
                # edge-to-edge gap: "$0M debt" (gap 1) beats "cash - $0M" (gap 3).
                # Measuring from the figure's start alone gets this backwards.
                gap = max(0, lm.start() - fig_end, fig_start - lm.end())
                if best_gap is None or gap < best_gap:
                    nearest, best_gap = (label, accepted), gap

        if nearest and not _matches(value, tol, nearest[1]) and not CONTEXT_RE.search(ctx):
            label, accepted = nearest
            mislabelled.append({
                "figure": fig["literal"], "labelled_as": label,
                "context": ctx,
                "expected_any_of": [round(a, 2) for a in accepted if a is not None][:6],
                "detail": f"{fig['literal']} is presented as '{label}' but no {label} value "
                          f"returned by any tool matches it.",
            })

        # --- check 1: does the number exist in tool output at all? ------------
        if not _matches(value, tol, tool_values):
            untraceable.append({"figure": fig["literal"], "context": ctx,
                                "detail": f"{fig['literal']} does not match any number "
                                          f"returned by any tool this run."})

    findings = {"figures_checked": len(extract_currency_figures(memo_text)),
                "mislabelled": mislabelled, "untraceable": untraceable}
    findings["clean"] = not (mislabelled or untraceable)
    findings["summary"] = (
        "All dollar figures trace to tool output and match their labels."
        if findings["clean"] else
        f"{len(mislabelled)} figure(s) attached to the wrong label, "
        f"{len(untraceable)} figure(s) not found in any tool result."
    )
    return findings


def build_labelled_values(company, ltm_revenue, ltm_ebitda, result, sources_uses) -> dict[str, list]:
    """
    Map label words to every value that could legitimately sit next to them.

    Each label carries its FULL series — all filed years plus all projected years —
    so "exit EBITDA: $91.4M" and "FY2021 revenue: $1.33B" both pass while a figure
    that belongs to no period at all is caught.
    """
    years = list(getattr(company, "years", []) or [])
    proj = list(getattr(result, "projections", []) or []) if result else []

    def series(attr, proj_attr=None):
        vals = [getattr(y, attr, None) for y in years]
        if proj_attr:
            vals += [getattr(p, proj_attr, None) for p in proj]
        return [v for v in vals if v is not None]

    labelled: dict[str, list] = {
        "cash": series("cash", "cash_balance_ending"),
        "revenue": series("revenue", "revenue") + ([ltm_revenue] if ltm_revenue else []),
        "net sales": series("revenue", "revenue") + ([ltm_revenue] if ltm_revenue else []),
        "ebitda": series("ebitda", "ebitda") + ([ltm_ebitda] if ltm_ebitda else []),
    }

    # Existing debt is $0 when SEC reports no debt tags; the memo says so explicitly.
    debt_vals = series("total_debt", "total_debt_ending") + [0.0]
    if sources_uses:
        debt_vals.append(sources_uses.total_new_debt)
        labelled.update({
            "sponsor equity": [sources_uses.sponsor_equity],
            "enterprise value": [sources_uses.purchase_enterprise_value]
                                + ([result.exit_enterprise_value] if result else []),
            "new debt": [sources_uses.total_new_debt],
            "transaction fees": [sources_uses.transaction_fees],
            "financing fees": [sources_uses.financing_fees],
        })
    labelled["debt"] = debt_vals

    if result:
        labelled.update({
            "exit equity": [result.exit_equity_value],
            "exit net debt": [result.exit_net_debt],
            "exit ebitda": [result.exit_ebitda],
        })
    return {k: v for k, v in labelled.items() if v}
