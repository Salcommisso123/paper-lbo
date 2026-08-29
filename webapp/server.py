"""
server.py — a thin web UI for PaperLBO.

Serves a single page with a ticker input and streams the agent's live reasoning
(the exact same tool-use loop the CLI runs) to the browser over Server-Sent
Events. It does NOT reimplement any agent logic — it consumes
`agent.iter_agent_events`, the single source of truth shared with the CLI. Every
number still comes out of the deterministic lbo_engine; this file only moves the
event stream and the finished workbook over HTTP.

Run it:
    uvicorn webapp.server:app --reload        # from the repo root
    # then open http://127.0.0.1:8000

Needs ANTHROPIC_API_KEY and EDGAR_USER_AGENT — loaded from .env automatically.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent

# Load .env (ANTHROPIC_API_KEY, EDGAR_USER_AGENT) before importing the agent.
load_dotenv(REPO / ".env")
sys.path.insert(0, str(REPO / "src"))

import agent  # noqa: E402  (import after sys.path + dotenv setup)

OUT_DIR = REPO / "output"
INDEX_PATH = ROOT / "index.html"

# $ per 1M tokens (input, output) — keep in sync with docs/COST_CONTROL.md.
PRICING = {
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-opus-4-8": (5.0, 25.0),
}

app = FastAPI(title="PaperLBO")


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    # Read per request: uvicorn --reload only watches .py, so caching this at import
    # meant edits to the UI never showed up without a manual restart.
    return INDEX_PATH.read_text()


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, default=str)}\n\n"


@app.get("/api/stream")
def stream(ticker: str = ""):
    """SSE endpoint: run the agent for `ticker` and stream its events."""
    ticker = (ticker or "").strip().upper()

    def gen():
        if not ticker:
            yield _sse({"type": "error", "message": "Enter a ticker symbol."})
            return
        try:
            for ev in agent.iter_agent_events(ticker, OUT_DIR):
                if ev.get("type") == "usage":
                    ev["cost_usd"] = _estimate_cost(ev)
                yield _sse(ev)
        except Exception as e:  # surface any unexpected failure instead of a dead stream
            yield _sse({"type": "error", "message": f"{type(e).__name__}: {e}"})

    # Cache-Control/X-Accel headers keep proxies from buffering the event stream.
    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _estimate_cost(usage_event: dict):
    rate = PRICING.get(usage_event.get("model", ""))
    if not rate:
        return None
    return round(usage_event["input_tokens"] * rate[0] / 1e6
                 + usage_event["output_tokens"] * rate[1] / 1e6, 4)


@app.get("/download/{filename}")
def download(filename: str):
    """Serve a generated workbook. Basename-only to prevent path traversal."""
    path = OUT_DIR / Path(filename).name
    if not path.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(
        path,
        filename=path.name,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
