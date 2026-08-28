# PaperLBO

*A "paper LBO" is the back-of-the-envelope LBO that PE firms make candidates
grind through by hand as an interview screen. PaperLBO automates their own
test — end to end, from a ticker to a formatted model.*

An autonomous Claude agent that builds a leveraged-buyout model for a public
company from real SEC filings — sources & uses, a multi-tranche debt
schedule with a cash sweep, five-year projections, exit returns (MOIC/IRR),
and a sensitivity grid — and outputs a formatted Excel workbook plus a
one-page investment memo.

Built as a portfolio project for private equity recruiting. It automates
what PE firms call a "paper LBO" — a standard interview screen — end to end:
give it a ticker, it fetches the filings, proposes and justifies its own
deal assumptions, runs the model, sanity-checks its own output, and hands
you back a workbook.

## A real run: SHOE (Shoe Station Group, formerly Shoe Carnival)

Built live from SEC EDGAR with the agent running on Claude Haiku 4.5 (~3.5¢ in
API cost). Workbook: [`examples/real_output/SHOE_LBO_Model.xlsx`](examples/real_output/SHOE_LBO_Model.xlsx).

| | |
| --- | --- |
| LTM revenue / EBITDA (FY2025 10-K) | $1.14B / $101M (~8.9% margin) |
| Entry | 6.5x EV/EBITDA (~$657M EV) |
| Leverage | 3.5x EBITDA, 80% cash sweep |
| Exit | 6.5x (no multiple expansion), 5-year hold |
| **Base-case return** | **1.58x MOIC / 9.6% IRR** |

The interesting part is what the agent *didn't* do: it flagged the 9.6% IRR as
below a typical PE hurdle, explained why (declining revenue, thin retail
margins, a debt-free target that limits the leverage lever), and said so plainly
rather than dressing up a marginal deal. Point it at `--ticker FLWS` and it
refuses to build a model at all, because 1-800-Flowers' latest EBITDA is
negative — the "flag the bad data instead of forcing a polished-looking model"
behavior is the whole point.

## Why this exists

Trying to build a real LBO for a lower-middle-market target quickly runs
into a wall: firms like Mangrove Equity Partners or KLH Capital buy private
companies, and private companies don't file public financials. So this
project starts where the data actually exists — public companies via SEC
EDGAR, which is free and complete — to prove out the mechanics correctly and
verifiably. A `v2` extension that estimates financials for private targets
from indirect signals is sketched in `docs/ROADMAP_V2_PRIVATE_TARGETS.md`.

## Architecture (the part worth explaining in an interview)

```
ticker
  │
  ▼
edgar.py  ───────────►  SEC EDGAR (data.sec.gov, free, no key)
  │  pulls 10-K XBRL tags, computes LTM EBITDA, flags bad/missing data
  ▼
agent.py  ───────────►  Claude (tool-use loop)
  │  picks & justifies assumptions, calls lbo_engine as a tool,
  │  sanity-checks the result, can revise once, writes the memo
  ▼
lbo_engine.py            (pure Python — no LLM here)
  │  sources & uses, debt schedule w/ cash sweep, projections, IRR/MOIC
  ▼
excel_writer.py  ────►  formatted .xlsx workbook
```

The one decision that matters most: **Claude never computes a number that
ends up in the model.** It orchestrates (decides what to fetch, what
assumptions to try, whether the result looks sane) and it writes English
(the memo). Every dollar figure, every IRR, comes out of `lbo_engine.py`,
which is plain, tested Python. That's what makes the output auditable
instead of "an LLM said so" — see `tests/test_lbo_engine.py`, which checks
the engine against a hand-calculated example.

## Setup

```bash
cd paper-lbo
pip install -r requirements.txt
cp .env.example .env   # fill in ANTHROPIC_API_KEY and EDGAR_USER_AGENT
export $(cat .env | xargs)   # or just export the two vars directly
```

Run it:

```bash
python src/agent.py --ticker SHOE
```

This prints the agent's reasoning and tool calls live, and writes
`output/SHOE_LBO_Model.xlsx` (or whatever filename Claude chooses).

Run without an API key at all (no cost, no network beyond SEC):

```bash
python src/edgar.py SHOE          # just pull and print the raw financials
python examples/generate_demo.py  # regenerate the sample workbook with fixed assumptions
```

Run the tests (fully offline, no API key or network needed):

```bash
python -m pytest tests/ -v
```

## What's in `examples/sample_output/`

A fully worked example workbook (`Example_Industrial_Services_LBO_Model.xlsx`)
built from representative sample financials — open this first if you just
want to see the output format before setting up an API key. It's clearly
labeled as illustrative, not a real filer.

## Cost

See `docs/COST_CONTROL.md`. Short version: SEC EDGAR is free, the only real
cost is Claude API usage (a few cents per run at typical token volumes), the
agent has a hard turn cap so it can't loop forever, and every run prints its
own token usage so nothing is a surprise. Set a spend limit in the Anthropic
Console before you start using this for real.

## Project layout

```
src/
  edgar.py         SEC EDGAR fetch + parse (no LBO logic)
  lbo_engine.py     deterministic LBO math (no LLM, no data-fetching)
  excel_writer.py   formats an LBOResult into a .xlsx workbook
  agent.py          the Claude tool-use loop that ties the above together
tests/
  test_lbo_engine.py    engine checked against hand-calculated numbers
  test_edgar_parser.py  SEC JSON parsing checked against a saved fixture
examples/
  generate_demo.py      builds the sample workbook with no API key needed
docs/
  ARCHITECTURE.md            design rationale, deeper than this README
  COST_CONTROL.md            how the "no surprise fees" goal is enforced
  ROADMAP_V2_PRIVATE_TARGETS.md   the private-target estimation stretch goal
```
