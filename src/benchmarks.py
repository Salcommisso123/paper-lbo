"""
benchmarks.py — sector benchmarks from NYU Stern's Damodaran datasets.

Free, no API key, no signup. Aswath Damodaran publishes industry-average data for
US companies as downloadable spreadsheets, refreshed each January:
    https://pages.stern.nyu.edu/~adamodar/New_Home_Page/data.html

We use two files:
    vebitda.xls — enterprise value multiples (EV/EBITDA, EV/EBIT) by industry
    margin.xls  — margins by industry, including EBITDA/Sales

This replaces the hardcoded "roughly 6-9x" heuristic in the agent's system prompt
with a real, citable number for the company's actual sector.

IMPORTANT — what these multiples are, and are not
-------------------------------------------------
Damodaran's EV/EBITDA is the aggregate multiple at which the PUBLIC EQUITY of an
industry trades: minority stakes, no control premium, and the whole sector's growth
names averaged in. An LBO entry multiple is a different thing — a negotiated price
for control of one company, and for a small, slow- or negative-growth target it is
routinely well below the sector's public trading multiple.

So this data is a reference point to reason against, NOT a target to match. If the
sector trades at 11.5x and the model enters at 6.5x, that gap is the analysis: it
should be explained (scale, growth, margin, cyclicality), not closed. The agent
prompt says so explicitly.

Best-effort by design: if Stern's server is unreachable or the sheet layout changes,
every function returns a status the agent can report and proceed from.
"""

from __future__ import annotations

import difflib
import time
from pathlib import Path
from typing import Optional

import requests

DATASETS = {
    "multiples": "https://pages.stern.nyu.edu/~adamodar/pc/datasets/vebitda.xls",
    "margins": "https://pages.stern.nyu.edu/~adamodar/pc/datasets/margin.xls",
}
SHEET = "Industry Averages"
HEADER_ROW = 8          # row index of the column headers; data starts at 9
CACHE_DIR = Path(__file__).resolve().parent.parent / ".cache"
CACHE_TTL = 30 * 24 * 3600   # the source updates annually; a month is plenty fresh

# Columns we read, by header index within `SHEET`.
MULTIPLE_COLS = {"n_firms": 1, "ev_ebitda": 3, "ev_ebit": 4, "ev_ebitda_all_firms": 7}
MARGIN_COLS = {"gross_margin": 2, "net_margin": 3, "operating_margin_pretax": 5,
               "ebitda_margin": 11}


def _download(name: str) -> Path:
    """Fetch a dataset to the local cache, reusing a recent copy when present."""
    CACHE_DIR.mkdir(exist_ok=True)
    path = CACHE_DIR / f"damodaran_{name}.xls"
    if path.exists() and (time.time() - path.stat().st_mtime) < CACHE_TTL:
        return path
    resp = requests.get(DATASETS[name], timeout=30,
                        headers={"User-Agent": "PaperLBO (educational project)"})
    resp.raise_for_status()
    path.write_bytes(resp.content)
    return path


def _read_rows(name: str) -> tuple[list[str], dict[str, list]]:
    """Return (industry names in file order, {industry_name: row_values})."""
    import xlrd  # imported lazily so a missing optional dep can't break a plain run

    sheet = xlrd.open_workbook(_download(name)).sheet_by_name(SHEET)
    rows, names = {}, []
    for r in range(HEADER_ROW + 1, sheet.nrows):
        values = sheet.row_values(r)
        industry = str(values[0]).strip()
        if not industry:
            continue
        names.append(industry)
        rows[industry] = values
    return names, rows


def _num(values: list, idx: int) -> Optional[float]:
    """Damodaran writes 'NA' into cells with no meaningful value."""
    try:
        raw = values[idx]
    except IndexError:
        return None
    if raw in ("", "NA", None):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


STOPWORDS = ("the", "and", "of", "lines", "general")
MIN_PREFIX = 4   # 'R.E.I.T.' tokenizes to r/e/i/t; without this every word prefix-matches it


def _tokens(text: str) -> list[str]:
    return [t for t in "".join(c if c.isalnum() else " " for c in text.lower()).split()
            if len(t) >= 3 and t not in STOPWORDS]


def _token_score(query: str, name: str) -> float:
    """
    Overlap between two labels' words, counting prefix matches so 'specialty retail'
    reaches 'Retail (Special Lines)' — Damodaran's labels rarely match how a person
    names a sector verbatim. Prefix matching needs both words to be reasonably long,
    or short/abbreviated industry names swallow every query.
    """
    q, n = _tokens(query), _tokens(name)
    if not q or not n:
        return 0.0
    def hit(a: str, b: str) -> bool:
        if a == b:
            return True
        return (min(len(a), len(b)) >= MIN_PREFIX
                and (a.startswith(b) or b.startswith(a)))
    return sum(1 for a in q if any(hit(a, b) for b in n)) / len(q)


def resolve_industry(query: str, names: list[str]) -> tuple[Optional[str], list[str]]:
    """
    Resolve a free-text sector name to one of Damodaran's ~96 industry labels.

    Returns (match, candidates). A match is only returned when it is UNAMBIGUOUS. When
    several industries fit equally well the match is None and the candidates are handed
    back for the caller to choose from — guessing here is worse than asking, because a
    silently wrong sector produces a confident, wrong benchmark. Both real failures
    looked exactly like this: "retail" resolved to "Retail (REITs)" for a footwear
    retailer because it was the shortest containing name, and "footwear retail" did the
    same on a tied token score.
    """
    q = query.strip().lower()
    for n in names:                                   # exact
        if n.lower() == q:
            return n, []

    contains = [n for n in names if q in n.lower() or n.lower() in q]
    if len(contains) == 1:
        return contains[0], []
    if len(contains) > 1:
        return None, contains

    scores = {n: _token_score(query, n) for n in names}
    top = max(scores.values())
    if top >= 0.5:                                    # at least half the query's words hit
        winners = [n for n, sc in scores.items() if sc == top]
        return (winners[0], []) if len(winners) == 1 else (None, winners)

    # Character-similarity is the last resort and a loose cutoff produces confidently
    # wrong matches ('Footwear' -> 'Power' at 0.6).
    close = difflib.get_close_matches(query, names, n=1, cutoff=0.8)
    return (close[0], []) if close else (None, [])


def match_industry(query: str, names: list[str]) -> Optional[str]:
    """The unambiguous match only, or None. See resolve_industry for the candidates."""
    return resolve_industry(query, names)[0]


def get_industry_benchmarks(industry: str) -> dict:
    """
    Look up sector-average valuation multiples and margins for `industry`.

    Returns the matched industry row plus the Total Market row for context. On any
    failure returns {"status": "unavailable", "reason": ...} so the caller can note
    the gap and carry on rather than aborting the run.
    """
    try:
        mult_names, mult_rows = _read_rows("multiples")
        marg_names, marg_rows = _read_rows("margins")
    except ImportError:
        return {"status": "unavailable",
                "reason": "The 'xlrd' package is needed to read Damodaran's .xls files "
                          "(pip install xlrd). Proceed without sector benchmarks."}
    except Exception as e:
        return {"status": "unavailable",
                "reason": f"Could not load NYU Stern datasets ({type(e).__name__}: {e}). "
                          "Proceed without sector benchmarks and say so in the memo."}

    matched, candidates = resolve_industry(industry, mult_names)
    if matched is None:
        if len(candidates) > 1:
            return {"status": "ambiguous", "query": industry, "candidates": candidates,
                    "reason": f"'{industry}' matches {len(candidates)} industries. Pick the "
                              f"one that fits this company and call again with it exactly — "
                              f"do not let this default, the wrong sector produces a "
                              f"misleading benchmark."}
        return {"status": "no_match", "query": industry,
                "reason": f"'{industry}' did not match any Damodaran industry.",
                "available_industries": mult_names,
                "hint": "Retry with one of the listed names — they are specific, e.g. "
                        "'Retail (Special Lines)' for specialty retail, not 'Retail'."}

    def pack(rows: dict, name: str, cols: dict) -> dict:
        values = rows.get(name)
        return {k: _num(values, i) for k, i in cols.items()} if values else {}

    sector = {**pack(mult_rows, matched, MULTIPLE_COLS),
              **pack(marg_rows, match_industry(matched, marg_names) or matched, MARGIN_COLS)}
    total = {**pack(mult_rows, "Total Market", MULTIPLE_COLS),
             **pack(marg_rows, "Total Market", MARGIN_COLS)}

    return {
        "status": "ok",
        "source": "NYU Stern / Damodaran industry averages, US companies "
                  "(pages.stern.nyu.edu/~adamodar), updated annually each January",
        "query": industry,
        "matched_industry": matched,
        "sector": sector,
        "total_market": total,
        "interpretation": (
            "These are PUBLIC-MARKET TRADING multiples for minority stakes across the "
            "whole sector, not LBO entry multiples. A control buyout of a small, "
            "low-growth target normally prices well BELOW the sector's trading multiple. "
            "Use this to explain where the entry multiple sits relative to the sector and "
            "why — do not move the entry multiple up to match it."
        ),
    }


if __name__ == "__main__":
    import json
    import sys

    print(json.dumps(get_industry_benchmarks(
        sys.argv[1] if len(sys.argv) > 1 else "Retail (Special Lines)"), indent=2)[:1800])
