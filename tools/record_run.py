"""
record_run.py — save one live agent run as a replayable fixture.

WHY
---
A complete run costs ~$0.10 in API credits (see docs/COST_CONTROL.md), and
iterating on the web UI — layout, a chart, a screenshot for the README — usually
takes several looks at a finished run. Paying per look is wasteful, and it stops
entirely when the API balance runs out.

Record once, replay for free:

    python tools/record_run.py --ticker SHOE          # costs one run
    uvicorn webapp.server:app                          # then, forever after:
    open "http://127.0.0.1:8000/?replay=SHOE"

The fixture is the exact event stream `agent.iter_agent_events` produced, so the
replay exercises the real client code path — same SSE events, same renderer. It
is a recording of a real run against real SEC data, not synthetic data.

Fixtures live in webapp/fixtures/ and are committed, so a fresh clone can see a
finished run without an API key at all.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO / ".env")

import agent  # noqa: E402

FIXTURE_DIR = REPO / "webapp" / "fixtures"


def record(ticker: str, out_dir: Path) -> Path:
    events: list[dict] = [{
        "type": "_meta",
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ticker": ticker.upper(),
        "model": agent.MODEL,
        "note": "Live run against SEC EDGAR, recorded verbatim from "
                "agent.iter_agent_events. Replay with /api/stream?replay=<name>.",
    }]

    started = time.monotonic()
    for event in agent.iter_agent_events(ticker, out_dir):
        record = json.loads(json.dumps(event, default=str))         # ensure serializable
        # Seconds since the run began, so a replay shows the timings the run really
        # had rather than 00:00 on every line.
        record["t"] = round(time.monotonic() - started, 1)
        events.append(record)
        kind = event["type"]
        if kind == "tool_call":
            print(f"  · {event['name']}")
        elif kind == "usage":
            print(f"  · {event['input_tokens']:,} in / {event['output_tokens']:,} out")
        elif kind == "error":
            print(f"  ! {event['message']}")

    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    path = FIXTURE_DIR / f"{ticker.upper()}.json"
    path.write_text(json.dumps(events, indent=1))
    return path


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ticker", required=True, help="e.g. SHOE")
    ap.add_argument("--out-dir", default=str(REPO / "output"),
                    help="where the run writes its workbook (default: output/)")
    args = ap.parse_args()

    print(f"Recording a live run for {args.ticker.upper()} — this costs API credits.")
    saved = record(args.ticker, Path(args.out_dir))
    events = json.loads(saved.read_text())
    print(f"\nSaved {len(events) - 1} events to {saved.relative_to(REPO)}")
    print(f"Replay: http://127.0.0.1:8000/?replay={args.ticker.upper()}")
