# Architecture notes

## Why split fetch / calculate / format / orchestrate into four files?

Because each piece has a different trust requirement:

- `edgar.py` (fetch): can fail loudly (missing tag, 404, bad ticker) — it's
  designed to flag uncertainty rather than silently substitute a guess. If
  D&A isn't tagged for a company, the code says so in `data_quality_flags`
  instead of quietly computing a wrong EBITDA.
- `lbo_engine.py` (calculate): must be exactly right, every time, for any
  input. It's pure functions over dataclasses, with no I/O and no
  randomness, which is what makes it unit-testable against hand-calculated
  numbers (see `tests/test_lbo_engine.py`).
- `excel_writer.py` (format): purely presentational. It never recalculates
  anything — it just formats numbers that already exist on an `LBOResult`.
- `agent.py` (orchestrate): the only file that talks to Claude. It owns
  judgment calls (what multiple is reasonable for this company) and prose
  (the memo), and nothing else — it hands every number off to
  `lbo_engine.py` via a tool call rather than typing a number itself.

## Why a tool-use loop instead of one big prompt

A single prompt that says "here's a company's financials, write me an LBO
model" would have Claude generating the arithmetic itself — which is
exactly the failure mode a PE reviewer will probe for ("did you actually
compute this, or did the model hallucinate a plausible-looking number?").
Wiring the calculation up as a callable tool means the arithmetic is
identical every time given the same inputs, and it's the kind of thing you
can point to a unit test for.

The self-correction step (propose assumptions -> check the result -> revise
once if it's unrealistic) is what makes this an *agent* rather than a
script with an LLM-shaped last step: it's making a judgment call, observing
the consequence, and adjusting — bounded by a one-revision cap so it can't
thrash indefinitely.

## Known simplifications (v1, on purpose)

These are standard "quick" / paper-LBO conventions, not oversights — they
keep the model auditable and fast to build, and every one of them is a
clearly labeled next step rather than a hidden inaccuracy:

- **Cash-free / debt-free deal convention** — the target's existing debt and
  cash aren't carried into the new capital structure. Standard for a
  first-pass model; a real deal would model existing debt refinancing
  explicitly.
- **Flat revenue growth and flat EBITDA margin** over the hold period,
  unless assumptions are overridden. No operational improvement thesis is
  modeled (e.g., margin expansion from cost cuts) — that's a natural v2
  addition once the base case works.
- **No revolver / no cash interest income** — retained (un-swept) free cash
  flow *is* accumulated as a cash balance that nets against debt at exit, so
  the model stays internally consistent at any sweep %, but that cash isn't
  assumed to earn interest, and there's no revolver draw for a bad year. Fine
  for a base case; matters more in a downside scenario.
- **Single exit, no interim dividends/recaps** — IRR is the clean
  `MOIC^(1/years) - 1`. A dividend recap would need a real cash-flow-dated
  IRR calculation (XIRR-style), which is a bounded, well-understood addition.
- **Cash sweep defaults to 80%** — real deals often sweep 75-90% and let the
  rest build a cash cushion, so 80% is the default rather than a 100% sweep
  (which pays debt down maximally and flatters returns). `cash_sweep_pct` is a
  parameter, so tune it for a specific case; retained cash accumulates and nets
  against exit debt, so changing it keeps the model consistent either way.
- **EBITDA add-backs aren't modeled** — the engine uses reported operating
  income + D&A. Real IC memos scrutinize seller-proposed add-backs hard;
  this tool reports the unadjusted figure and flags it as a "verify before
  this goes further" item in the memo, rather than guessing at adjustments.
