"""
edgar.py — free SEC EDGAR data fetching for PaperLBO.

Uses SEC's public XBRL "frames"/"companyconcept" APIs (data.sec.gov).
No API key required — SEC only asks that every request carry a descriptive
User-Agent with a name + contact email (their fair-access policy). Set
EDGAR_USER_AGENT in your environment or .env file before running.

Docs: https://www.sec.gov/edgar/sec-api-documentation

Design note: this module ONLY fetches and normalizes raw financial data.
It never does LBO math — that lives in lbo_engine.py, on purpose, so the
numbers a recruiter sees are produced by deterministic, auditable Python,
not by an LLM doing arithmetic.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import requests

SEC_HEADERS_DEFAULT_UA = "PaperLBO-Demo contact@example.com  (SET EDGAR_USER_AGENT env var)"
TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
COMPANYCONCEPT_URL = "https://data.sec.gov/api/xbrl/companyconcept/CIK{cik10}/us-gaap/{tag}.json"
COMPANYCONCEPT_DEI_URL = "https://data.sec.gov/api/xbrl/companyconcept/CIK{cik10}/dei/{tag}.json"

CACHE_DIR = Path(__file__).resolve().parent.parent / ".cache"

# Candidate XBRL tags per concept, tried in order — companies tag the same
# line item differently (and tagging conventions shifted around 2018 with
# ASC 606 revenue recognition), so we fall back down this list.
TAG_CANDIDATES = {
    "revenue": [
        "Revenues",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "SalesRevenueNet",
        "SalesRevenueGoodsNet",
    ],
    "operating_income": ["OperatingIncomeLoss"],
    "depreciation_amortization": [
        "DepreciationDepletionAndAmortization",
        "DepreciationAmortizationAndAccretionNet",
        "DepreciationAndAmortization",
        "Depreciation",
    ],
    "interest_expense": [
        "InterestExpense",
        "InterestExpenseDebt",
        "InterestIncomeExpenseNet",
    ],
    "income_tax_expense": ["IncomeTaxExpenseBenefit"],
    "net_income": ["NetIncomeLoss", "ProfitLoss"],
    "cash": [
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
    ],
    "long_term_debt_noncurrent": ["LongTermDebtNoncurrent"],
    "long_term_debt_current": ["LongTermDebtCurrent"],
    "short_term_borrowings": ["ShortTermBorrowings", "DebtCurrent"],
    # Opening balance-sheet items — needed to build a balance sheet that ties, not just
    # an income statement and a cash flow.
    "ppe_net": ["PropertyPlantAndEquipmentNet"],
    "current_assets": ["AssetsCurrent"],
    "current_liabilities": ["LiabilitiesCurrent"],
    "capex": [
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsForCapitalImprovements",
    ],
}


def _headers() -> dict:
    ua = os.environ.get("EDGAR_USER_AGENT", SEC_HEADERS_DEFAULT_UA)
    if "SET EDGAR_USER_AGENT" in ua:
        raise RuntimeError(
            "Set the EDGAR_USER_AGENT environment variable before hitting SEC EDGAR, "
            "e.g. 'Sal Commisso sal@example.com'. SEC requires a real contact string "
            "on every request — see https://www.sec.gov/edgar/sec-api-documentation"
        )
    return {"User-Agent": ua, "Accept-Encoding": "gzip, deflate"}


def _get_json(url: str, max_retries: int = 3) -> Optional[dict]:
    """
    GET a SEC JSON endpoint. Returns None on a genuine 404 (concept/tag not
    reported by this filer — an expected, non-error outcome we fall through on).

    Retries transient failures (read/connect timeouts, 429 rate-limit, 5xx) with
    exponential backoff. We now issue ~30 requests per company (every candidate
    tag of every concept is fetched so we can merge coverage per year), so a single
    slow response from SEC's free API shouldn't abort the whole fetch.
    """
    backoff = 1.0
    last_err: Optional[Exception] = None
    for attempt in range(max_retries):
        try:
            resp = requests.get(url, headers=_headers(), timeout=30)
            time.sleep(0.15)  # be polite to SEC's free API — well under their 10 req/sec limit
            if resp.status_code == 404:
                return None
            if resp.status_code == 429 or resp.status_code >= 500:
                # transient server-side conditions — back off and retry
                last_err = requests.HTTPError(f"{resp.status_code} from SEC for {url}")
                time.sleep(backoff)
                backoff *= 2
                continue
            resp.raise_for_status()
            return resp.json()
        except (requests.Timeout, requests.ConnectionError) as e:
            last_err = e
            time.sleep(backoff)
            backoff *= 2
    raise RuntimeError(
        f"SEC request failed after {max_retries} attempts: {url} ({last_err})"
    )


def _ticker_map() -> dict:
    """Ticker -> CIK lookup, cached locally (the file is ~1MB and rarely changes)."""
    CACHE_DIR.mkdir(exist_ok=True)
    cache_file = CACHE_DIR / "company_tickers.json"
    if cache_file.exists() and (time.time() - cache_file.stat().st_mtime) < 7 * 24 * 3600:
        data = json.loads(cache_file.read_text())
    else:
        data = _get_json(TICKER_MAP_URL)
        cache_file.write_text(json.dumps(data))
    # file is shaped {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}, ...}
    return {row["ticker"].upper(): row for row in data.values()}


def resolve_cik(ticker: str) -> tuple[str, str]:
    """Return (cik10 zero-padded, company title) for a ticker."""
    tmap = _ticker_map()
    row = tmap.get(ticker.upper())
    if row is None:
        raise ValueError(f"Ticker '{ticker}' not found in SEC's company_tickers.json")
    return f"{int(row['cik_str']):010d}", row["title"]


def extract_annual_series(data: Optional[dict], taxonomy: str = "us-gaap") -> list[dict]:
    """
    Pure parsing function: given a raw SEC companyconcept JSON blob (already
    fetched), return one entry per fiscal year of 10-K annual data, deduped
    by fiscal year (keeping the most recently *filed* value, so restatements
    win). Kept separate from the HTTP call so it's unit-testable offline
    against a saved fixture — see tests/test_edgar_parser.py.
    """
    if not data:
        return []
    unit_key = "USD" if taxonomy == "us-gaap" else next(iter(data.get("units", {})), None)
    entries = data.get("units", {}).get(unit_key, []) if unit_key else []
    annual: dict[int, dict] = {}
    for e in entries:
        if e.get("form") not in ("10-K", "10-K/A"):
            continue
        if e.get("fp") != "FY":
            continue
        fy = e.get("fy")
        if fy is None:
            continue
        # A single 10-K reports the current year plus two prior comparative years,
        # and SEC tags ALL of them with that filing's fy (not each period's own fy).
        # So for a given fy we want the *current*-year figure, which is the entry
        # with the latest period-end date. Tie-break: most-recently-filed wins
        # (a 10-K/A restatement supersedes the original), then latest end. Sorting
        # on (filed, end) makes this independent of the array's ordering.
        prev = annual.get(fy)
        key = (e.get("filed", ""), e.get("end", ""))
        if prev is None or key >= (prev.get("filed", ""), prev.get("end", "")):
            annual[fy] = e
    return [annual[fy] for fy in sorted(annual)]


def _get_concept_series(cik10: str, tag: str, taxonomy: str = "us-gaap") -> list[dict]:
    url_tmpl = COMPANYCONCEPT_URL if taxonomy == "us-gaap" else COMPANYCONCEPT_DEI_URL
    data = _get_json(url_tmpl.format(cik10=cik10, tag=tag))
    return extract_annual_series(data, taxonomy)


def _merge_concept_by_year(cik10: str, concept: str, taxonomy: str = "us-gaap") -> tuple[dict[int, float], list[str]]:
    """
    Build {fiscal_year: value} for a concept by MERGING across its candidate tags,
    filling each year from the highest-priority tag that has data for that year.

    This replaces an earlier "use the first tag that has ANY data" approach, which
    broke on two real-world patterns:
      - A stale tag shadows a good one. 1-800-Flowers (FLWS) still exposes an old
        `Revenues` tag containing only FY2018, so "first non-empty tag wins" locked
        onto 2018 and returned nothing for 2019+, even though
        `RevenueFromContractWithCustomerExcludingAssessedTax` has every recent year.
      - A filer switches tags mid-history. Dine Brands (DIN) reports D&A and interest
        under different tags in older vs. newer 10-Ks, so no single tag covers all years.
    Merging per-year is robust to both: earlier candidates take priority, later ones
    only fill the years still missing. Returns the merged map plus the list of tags
    that actually contributed (for data-quality reporting).
    """
    merged: dict[int, float] = {}
    contributing: list[str] = []
    for tag in TAG_CANDIDATES[concept]:
        series = _get_concept_series(cik10, tag, taxonomy)
        added = False
        for row in series:
            fy = row["fy"]
            if fy not in merged:
                merged[fy] = row["val"]
                added = True
        if added:
            contributing.append(tag)
    return merged, contributing


@dataclass
class FiscalYearFinancials:
    fy: int
    revenue: Optional[float] = None
    operating_income: Optional[float] = None
    d_and_a: Optional[float] = None
    ebitda: Optional[float] = None
    ebitda_source: str = ""  # "operating_income+da" or "net_income_buildup"
    interest_expense: Optional[float] = None
    income_tax_expense: Optional[float] = None
    net_income: Optional[float] = None
    cash: Optional[float] = None
    total_debt: Optional[float] = None
    capex: Optional[float] = None
    ppe_net: Optional[float] = None
    current_assets: Optional[float] = None
    current_liabilities: Optional[float] = None
    flags: list[str] = field(default_factory=list)


@dataclass
class CompanyFinancials:
    ticker: str
    cik: str
    company_name: str
    years: list[FiscalYearFinancials]
    data_quality_flags: list[str] = field(default_factory=list)

    def latest_complete_year(self) -> Optional[FiscalYearFinancials]:
        for fyd in reversed(self.years):
            if fyd.revenue and fyd.ebitda is not None:
                return fyd
        return None


def non_cash_nwc(year: "FiscalYearFinancials") -> Optional[float]:
    """
    Net working capital excluding cash: (current assets - cash) - current liabilities.

    Cash is excluded because an LBO is priced cash-free/debt-free and the model tracks
    cash separately on the balance sheet; leaving it in would double-count it.
    """
    if year.current_assets is None or year.current_liabilities is None:
        return None
    return (year.current_assets - (year.cash or 0.0)) - year.current_liabilities


def assess_modelability(company: CompanyFinancials) -> dict:
    """
    Split data-quality findings into what BLOCKS a model and what is only a caveat.

    SEC XBRL tagging is patchy: filers tag the same line item differently and often omit
    tags entirely. A missing tag is normal and does not make a company unmodelable — only
    the absence of usable LTM EBITDA does. Leaving that judgment to the model as prose
    ("if the data-quality flags say the data is bad, stop") made it decline Shoe Carnival,
    a perfectly modelable company, because the long-term-debt tags were absent. The
    verdict is computed here so it is deterministic and testable.
    """
    latest = company.latest_complete_year()
    blocking: list[str] = []
    if not company.years:
        blocking.append("No 10-K annual XBRL data found for this ticker at all.")
    elif latest is None:
        blocking.append("No fiscal year has both revenue and EBITDA — nothing to model from.")
    elif latest.ebitda is not None and latest.ebitda <= 0:
        blocking.append(f"LTM EBITDA is non-positive (${latest.ebitda:,.0f}). An LBO needs "
                        f"positive cash earnings to service debt; modelling this would be "
                        f"dishonest, not conservative.")

    caveats = [f for f in company.data_quality_flags]
    if latest is not None:
        caveats += [f for f in latest.flags if "non-positive" not in f]

    return {
        "modelable": not blocking,
        "blocking": blocking,
        "caveats": caveats,
        "note": ("Caveats are disclosures, not reasons to decline. Build the model and "
                 "state them in the memo." if not blocking else
                 "Do not build a model. Explain the blocking issue plainly and stop."),
    }


def fetch_company_financials(ticker: str, lookback_years: int = 5) -> CompanyFinancials:
    """
    Pull the last `lookback_years` of 10-K annual figures for `ticker` from
    SEC EDGAR and normalize into per-fiscal-year records with EBITDA computed.
    Pure data-fetch — no assumptions, no valuation logic here.
    """
    cik10, company_name = resolve_cik(ticker)

    series = {}
    used_tags = {}
    for concept in TAG_CANDIDATES:
        merged, contributing = _merge_concept_by_year(cik10, concept)
        series[concept] = merged
        used_tags[concept] = ", ".join(contributing)  # "" if no candidate tag had data

    all_fys = sorted(set().union(*[set(v.keys()) for v in series.values()]))
    recent_fys = all_fys[-lookback_years:] if all_fys else []

    years: list[FiscalYearFinancials] = []
    for fy in recent_fys:
        fyd = FiscalYearFinancials(fy=fy)
        fyd.revenue = series["revenue"].get(fy)
        fyd.operating_income = series["operating_income"].get(fy)
        fyd.d_and_a = series["depreciation_amortization"].get(fy)
        fyd.interest_expense = series["interest_expense"].get(fy)
        fyd.income_tax_expense = series["income_tax_expense"].get(fy)
        fyd.net_income = series["net_income"].get(fy)
        fyd.cash = series["cash"].get(fy)
        fyd.capex = series["capex"].get(fy)
        fyd.ppe_net = series["ppe_net"].get(fy)
        fyd.current_assets = series["current_assets"].get(fy)
        fyd.current_liabilities = series["current_liabilities"].get(fy)

        ltd_nc = series["long_term_debt_noncurrent"].get(fy, 0) or 0
        ltd_c = series["long_term_debt_current"].get(fy, 0) or 0
        st_borrow = series["short_term_borrowings"].get(fy, 0) or 0
        total_debt = ltd_nc + ltd_c + st_borrow
        fyd.total_debt = total_debt if total_debt else None

        # EBITDA: prefer Operating Income + D&A. Fall back to a net-income buildup.
        if fyd.operating_income is not None and fyd.d_and_a is not None:
            fyd.ebitda = fyd.operating_income + fyd.d_and_a
            fyd.ebitda_source = "operating_income + D&A"
        elif fyd.net_income is not None and fyd.interest_expense is not None and \
                fyd.income_tax_expense is not None and fyd.d_and_a is not None:
            fyd.ebitda = fyd.net_income + fyd.interest_expense + fyd.income_tax_expense + fyd.d_and_a
            fyd.ebitda_source = "net_income + interest + tax + D&A (buildup, less reliable)"
        else:
            missing = [name for name, val in (
                ("operating income", fyd.operating_income),
                ("D&A", fyd.d_and_a),
                ("net income", fyd.net_income),
                ("interest expense", fyd.interest_expense),
                ("income tax", fyd.income_tax_expense),
            ) if val is None]
            fyd.flags.append(
                "EBITDA could not be computed — need either (operating income + D&A) or "
                "(net income + interest + tax + D&A); missing: " + ", ".join(missing)
            )

        if fyd.ebitda is not None and fyd.ebitda <= 0:
            fyd.flags.append(f"EBITDA is non-positive (${fyd.ebitda:,.0f}) — not a clean LBO candidate as-is")
        if fyd.d_and_a is None:
            fyd.flags.append("D&A tag not found — EBITDA may be understated if buildup path was used")
        if fyd.total_debt is None:
            fyd.flags.append("No long-term-debt tags found — treating existing debt as $0 (verify manually)")

        years.append(fyd)

    cf = CompanyFinancials(ticker=ticker.upper(), cik=cik10, company_name=company_name, years=years)
    if not years:
        cf.data_quality_flags.append("No 10-K annual XBRL data found for this ticker at all.")
    missing_tags = [c for c, t in used_tags.items() if not t]
    if missing_tags:
        cf.data_quality_flags.append(f"No XBRL tag matched for: {', '.join(missing_tags)}")
    return cf


if __name__ == "__main__":
    import sys

    tkr = sys.argv[1] if len(sys.argv) > 1 else "AAPL"
    result = fetch_company_financials(tkr)
    print(f"{result.company_name} ({result.ticker}), CIK {result.cik}")
    for y in result.years:
        print(
            f"  FY{y.fy}: rev={y.revenue}, EBITDA={y.ebitda} ({y.ebitda_source}), "
            f"debt={y.total_debt}, cash={y.cash}, flags={y.flags}"
        )
