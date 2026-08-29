# Cost control

The goal is zero surprises. Here's every place money could enter the
picture, and what's done about each:

## Data

SEC EDGAR (`data.sec.gov`) is free, public, and requires no API key —
just a descriptive User-Agent header, which is a courtesy requirement, not
a billing relationship. There is no paid data provider anywhere in this
project. If a future version adds one (see `ROADMAP_V2_PRIVATE_TARGETS.md`),
it should be opt-in and require an explicit, known, capped price before
it's wired in — never added quietly.

## The Claude API

This is the only real cost, and it's pay-as-you-go against your own
Anthropic API key — nothing recurring, nothing that charges you without a
run happening.

Concretely, per run:
- A handful of tool-use turns (typically 6-9 now that the verification,
  benchmark and MD&A tools run before the memo), each sending the running
  conversation plus tool results. A complete run measured 53K-93K input and
  4.0K-11.9K output tokens — about $0.07-$0.15 on Haiku 4.5, so cents rather
  than dollars, but roughly 3x what it was before those tools existed. Check
  current per-token pricing for whatever model you configure, since it's not
  baked into this repo (rates change):
  https://docs.claude.com/en/docs/about-claude/models
- The MD&A excerpts are the single largest contributor to input tokens. If you
  need runs cheaper, that is the first thing to trim — `filings.py` caps them at
  `MAX_EXCERPT_CHARS`.
- `agent.py` prints total input/output tokens used at the end of every run,
  so you always see exactly what a run cost before you run it again.
- `LBO_AGENT_MAX_TURNS` (default 12, raised from 8 when the grounding tools
  were added) hard-stops the tool-use loop even if something goes wrong — a
  12-turn run costs at most 12x a single call, it can't spiral.
- `LBO_AGENT_MAX_TOKENS` (default 3000) caps the size of each individual
  response.

## Which model to default to

Verified against docs.claude.com pricing (checked 2026-08-28). Pick the
cheapest model that still writes an acceptable memo; the calculation engine is
deterministic Python, so the model choice only affects the *prose* and the
*assumption judgment*, never the numbers.

| Model | ID (`LBO_AGENT_MODEL`) | Input / Output per 1M tok | Approx. cost per run* |
| --- | --- | --- | --- |
| **Haiku 4.5** (default) | `claude-haiku-4-5` | $1 / $5 | **~$0.10** |
| Sonnet 5 | `claude-sonnet-5` | $3 / $15 (intro $2 / $10 thru 2026-08-31) | ~$0.31 (~$0.20 at the intro rate) |
| Opus 4.8 | `claude-opus-4-8` | $5 / $25 | ~$0.51 |

\* Rates above x a measured run: SHOE on Haiku 4.5 used 67.4K input + 7.0K output
tokens across its tool-use turns. Sonnet/Opus token counts are similar; the cost
scales with the per-token rate. Runs range $0.07-$0.15 on Haiku depending on how
many times the agent revises — the top of that range is a run where the memo
figure audit rejected the first draft and it rewrote the memo.

**Recommendation: default to Haiku 4.5.** On live SHOE runs it fetched the data,
proposed and justified reasonable assumptions against sector benchmarks, correctly
flagged a below-hurdle IRR, and wrote a clean memo — for about 10 cents. That's the
default baked into `agent.py`.

**Step up to Sonnet 5 for the polished version you actually send to a
recruiter** — noticeably better prose and slightly sharper assumption judgment, at
roughly 3x the cost (~$0.31/run, or ~$0.20 while the introductory rate lasts):
`LBO_AGENT_MODEL=claude-sonnet-5 python src/agent.py --ticker <T>`. Iterate on
Haiku, do the final run on Sonnet.
Opus 4.8 is available if you want the strongest reasoning, but it's overkill for
a paper LBO — the memo quality gain over Sonnet 5 doesn't justify the cost here.

Model names and prices change — re-check https://docs.claude.com/en/docs/about-claude/models
before relying on the figures above.

## Before running this for real

1. In the Anthropic Console (console.anthropic.com), set a monthly spend
   limit under Billing. This is the actual backstop — even if something in
   this code were wrong, your account can't be charged past that number.
2. Start with the cheapest model that gives acceptable memo quality
   (`LBO_AGENT_MODEL` env var) — you can always bump it up for the version
   you actually send to a recruiter.
3. Run `examples/generate_demo.py` first — it exercises the entire
   Excel-formatting pipeline with zero API calls, so you can confirm the
   output looks right before spending anything on live runs.

## Hosting / infrastructure

None required for the CLI version. If you build the optional Excel/repo
combo into a web app later, that reintroduces a hosting cost — budget for
it explicitly rather than picking a host that surprises you with usage-based
billing.
