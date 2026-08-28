# v2 idea: a "no public financials" mode

The problem that started this project: firms like Mangrove Equity Partners
or KLH Capital buy private, lower-middle-market companies, and there's no
10-K to pull. This is the differentiated version of this project — build it
once v1 (the public-company agent) is solid and demoable.

## The honest framing

Nothing here produces "real" financials for a private company — it produces
a *documented estimate*, and the entire value of this mode is making that
estimation methodology transparent and defensible rather than a black box.
The Excel output and memo should say, explicitly, "this is an estimate
built from X, Y, Z proxies" — never present an estimate as if it were a
filed number.

## Plausible estimation proxies, roughly in order of defensibility

1. **Industry + employee count -> revenue proxy.** Public sources like
   state business registries, LinkedIn company size ranges, or
   D&B-style "revenue range" listings give a rough band. Revenue-per-employee
   benchmarks vary a lot by industry (services vs. manufacturing vs.
   distribution), so this needs an industry-specific multiplier table, not
   one global number.
2. **Public comps -> margin proxy.** Once you have a revenue estimate and an
   industry, borrow an EBITDA margin range from public comps in the same
   sub-industry (small-cap, similar business model) rather than guessing at
   a margin from nothing.
3. **Deal-size heuristics from PE firm profiles.** Firms like Mangrove and
   KLH publish target-deal criteria (revenue range, EBITDA range, sector
   focus) on their own sites — that's a legitimate, citable anchor for
   "what size company would this firm actually be looking at," even without
   that specific company's numbers.
4. **News / press-release mentions** — an acquisition announcement,
   trade-press coverage, or a company's own marketing sometimes states
   revenue or headcount directly. Worth a targeted search step before
   falling back to proxies 1-3.

## Suggested build shape

- A new `estimate.py` module, structurally parallel to `edgar.py`: takes an
  industry + rough size signal, returns an estimated `CompanyFinancials`-like
  object with a `confidence` field and a `methodology` string per line item
  — never a bare number with no explanation of where it came from.
- The agent's memo for this mode should have its own template section: "How
  these financials were estimated" before the investment case, not buried
  at the end.
- The Excel workbook should visually distinguish estimated inputs from
  calculated outputs (e.g., a distinct fill color) so nobody mistakes one
  for the other at a glance — the existing `RED_FLAG` fill pattern in
  `excel_writer.py` is a reasonable starting point for a new "ESTIMATED"
  style.

## Why this was deliberately deferred past v1

Estimates for a private target can't be checked against a filed number, so
they're much easier to get subtly wrong in a way nobody catches — including
you, in an interview, if someone asks a follow-up question you can't
answer. Shipping the verifiable public-company version first, well-tested,
is what makes this mode credible on top of it rather than instead of it.
