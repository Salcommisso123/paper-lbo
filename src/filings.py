"""
filings.py — pull what management actually SAID out of the latest 10-K.

The numbers in this project are already grounded (SEC XBRL -> deterministic engine).
The narrative was not: without this module the agent has to infer *why* margins moved
from the shape of the numbers alone, which produces plausible-sounding explanations
that no filing supports. This lets the memo quote or paraphrase the company's own
Management's Discussion & Analysis instead.

Two free SEC endpoints, no API key:
    efts.sec.gov/LATEST/search-index   full-text search across filings (2001+)
    www.sec.gov/Archives/...           the filing documents themselves

Returns short, targeted excerpts rather than the whole section — an Item 7 runs tens
of thousands of words and would swamp the agent's context for no benefit.

Best-effort by design: any failure returns a status the agent can report and proceed
from. A memo with no management quotes is fine; a failed run is not.
"""

from __future__ import annotations

import re
from html import unescape
from typing import Optional

import requests

from edgar import _headers

EFTS_URL = "https://efts.sec.gov/LATEST/search-index"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{document}"

MAX_EXCERPT_CHARS = 4000
WINDOW_CHARS = 700


# Filings use typographic punctuation ("MANAGEMENT’S"). Fold it to ASCII so the
# section regexes below can be written with plain quotes.
_PUNCT = {"‘": "'", "’": "'", "“": '"', "”": '"', "′": "'"}


def _strip_html(html: str) -> str:
    """Crude but dependency-free HTML -> text. Good enough to find and read Item 7."""
    text = re.sub(r"(?is)<(script|style|table)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = unescape(text)   # covers &nbsp;, &#160;, &#8217; and the rest in one pass
    for fancy, plain in _PUNCT.items():
        text = text.replace(fancy, plain)
    return re.sub(r"[ \t\xa0]+", " ", re.sub(r"\n\s*\n+", "\n\n", text)).strip()


def find_latest_10k(cik10: str, query: str = "discussion and analysis of financial condition") -> dict:
    """Locate the company's most recent 10-K document via SEC full-text search."""
    try:
        resp = requests.get(EFTS_URL, headers=_headers(), timeout=30,
                            params={"q": f'"{query}"', "forms": "10-K", "ciks": cik10})
        resp.raise_for_status()
        hits = resp.json().get("hits", {}).get("hits", [])
    except Exception as e:
        return {"status": "unavailable",
                "reason": f"SEC full-text search unreachable ({type(e).__name__}: {e})."}

    if not hits:
        return {"status": "not_found",
                "reason": "No 10-K matched in SEC full-text search (it only covers 2001 onward)."}

    latest = max(hits, key=lambda h: h.get("_source", {}).get("file_date", ""))
    accession, _, document = latest["_id"].partition(":")
    return {
        "status": "ok",
        "accession": accession,
        "document": document,
        "filed": latest.get("_source", {}).get("file_date"),
        "period_ending": latest.get("_source", {}).get("period_ending"),
        "url": ARCHIVE_URL.format(cik=int(cik10), accession=accession.replace("-", ""),
                                  document=document),
    }


def extract_mdna(text: str) -> Optional[str]:
    """
    Slice Item 7 (MD&A) out of a 10-K's text.

    'Item 7' appears many times in a 10-K: the table of contents, the real section
    heading, and cross-references in the prose ("see ITEM 7, 'Management's...'").

    Two rules separate them:
      - A real heading is followed by a period or colon ("ITEM 7. MANAGEMENT'S");
        a cross-reference uses a comma ("ITEM 7, 'Management's"), and "ITEM 8 of this
        Annual Report" has no delimiter at all. Requiring [.:] drops both.
      - Of the surviving candidates, the body is the LONGEST span from a start to the
        next section heading — the ToC entry closes almost immediately.

    Word-splitting from stripped inline markup ("FINANCIAL STATEME NTS") means the
    heading TEXT after the number is unreliable, so we key off the item number alone.
    """
    starts = [m.start() for m in
              re.finditer(r"(?i)item\s*7\s*[.:]\s*[\"']?\s*management", text)]
    ends = [m.start() for m in re.finditer(r"(?i)item\s*(?:7a|8)\s*[.:]", text)]
    best = None
    for s in starts:
        following = [e for e in ends if e > s]
        if not following:
            continue
        span = text[s:min(following)]
        if best is None or len(span) > len(best):
            best = span
    return best


def _find_topic(mdna: str, topic: str) -> Optional[int]:
    """
    Locate `topic` in the MD&A, loosening the match in stages. Filings rarely use an
    analyst's exact phrasing — a search for "comparable store sales" has to reach
    "comparable stores Net Sales" — so fall back from the phrase, to all of its words
    appearing close together, to its most distinctive single word.
    """
    exact = re.search(re.escape(topic), mdna, re.I)
    if exact:
        return exact.start()

    words = [w for w in re.findall(r"[A-Za-z]{4,}", topic)]
    if not words:
        return None

    # all words within one window, anchored on occurrences of the rarest-looking word
    anchor = max(words, key=len)
    for m in re.finditer(re.escape(anchor), mdna, re.I):
        window = mdna[max(0, m.start() - WINDOW_CHARS // 2): m.start() + WINDOW_CHARS // 2]
        if all(re.search(re.escape(w), window, re.I) for w in words):
            return m.start()

    single = re.search(re.escape(anchor), mdna, re.I)
    return single.start() if single else None


def get_management_discussion(cik10: str, topics: Optional[list[str]] = None) -> dict:
    """
    Return short MD&A excerpts from the latest 10-K, focused on `topics`
    (e.g. ["margin", "gross profit", "comparable sales"]).

    Best-effort: on any failure returns {"status": ...} with a reason, never raises.
    """
    located = find_latest_10k(cik10)
    if located.get("status") != "ok":
        return located

    try:
        resp = requests.get(located["url"], headers=_headers(), timeout=45)
        resp.raise_for_status()
        text = _strip_html(resp.text)
    except Exception as e:
        return {"status": "unavailable", "filing": located,
                "reason": f"Could not fetch the filing document ({type(e).__name__}: {e})."}

    mdna = extract_mdna(text)
    if not mdna:
        return {"status": "not_found", "filing": located,
                "reason": "Could not locate Item 7 in the filing — layouts vary. "
                          "Write the narrative without management's own wording, and say so."}

    excerpts, used = [], []
    for topic in (topics or []):
        hit = _find_topic(mdna, topic)
        if hit is None:
            continue
        start = max(0, hit - WINDOW_CHARS // 3)
        if any(abs(start - u) < WINDOW_CHARS for u in used):
            continue              # skip windows that would mostly repeat one already taken
        used.append(start)
        excerpts.append({"topic": topic, "text": mdna[start:start + WINDOW_CHARS].strip()})
        if sum(len(e["text"]) for e in excerpts) > MAX_EXCERPT_CHARS:
            break

    if not excerpts:
        excerpts = [{"topic": "opening of MD&A",
                     "text": mdna[:MAX_EXCERPT_CHARS // 2].strip()}]

    return {
        "status": "ok",
        "filing": located,
        "mdna_chars": len(mdna),
        "excerpts": excerpts,
        "citation_note": (
            f"Quotes come from the 10-K filed {located.get('filed')} for the period ending "
            f"{located.get('period_ending')} ({located['url']}). These are excerpts, not the "
            f"full section — attribute them to management and do not extrapolate beyond them."
        ),
    }


if __name__ == "__main__":
    import json
    import sys

    from edgar import resolve_cik

    cik, _ = resolve_cik(sys.argv[1] if len(sys.argv) > 1 else "SHOE")
    print(json.dumps(get_management_discussion(cik, ["gross profit margin", "comparable"]),
                     indent=2)[:2500])
