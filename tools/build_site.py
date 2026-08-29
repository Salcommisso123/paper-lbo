"""
build_site.py — assemble the static demo published to GitHub Pages.

WHY A STATIC BUILD
------------------
The FastAPI server exists to stream a LIVE agent run. A recorded run is just a saved
list of events, and the browser can pace those itself — so the public demo needs no
backend at all. That matters for three reasons:

  - No ANTHROPIC_API_KEY on any server, so a public URL cannot spend API credits.
    /api/stream has no auth; deployed live it would be an open invitation.
  - No cold start. Free container tiers sleep and wake in ~50 seconds; a recruiter
    following a link sees a blank page. Static pages are instant.
  - No hosting bill, ever.

The site is the same index.html and the same renderer as the local app. Only the
transport differs: window.PAPERLBO_STATIC makes the page fetch a fixture instead of
opening an SSE connection.

    python tools/build_site.py      # -> site/
    python -m http.server -d site   # preview at http://localhost:8000
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WEBAPP = REPO / "webapp"
FIXTURES = WEBAPP / "fixtures"
SITE = REPO / "site"

# Keep in sync with webapp/server.py PRICING.
PRICING = {"claude-haiku-4-5": (1.0, 5.0), "claude-haiku-4-5-20251001": (1.0, 5.0),
           "claude-sonnet-5": (3.0, 15.0), "claude-opus-4-8": (5.0, 25.0)}

# Workbooks the download button can serve. A fixture's summary names the file it
# produced; that name has to resolve to something committed, because the live run's
# output/ directory does not exist on a static host.
WORKBOOKS = [
    REPO / "examples" / "real_output" / "SHOE_LBO_Model.xlsx",
    REPO / "examples" / "sample_output" / "Example_Industrial_Services_LBO_Model.xlsx",
]


def build() -> Path:
    if SITE.exists():
        shutil.rmtree(SITE)
    (SITE / "fixtures").mkdir(parents=True)
    (SITE / "files").mkdir(parents=True)

    names = sorted(p.stem for p in FIXTURES.glob("*.json"))
    if not names:
        raise SystemExit("No fixtures in webapp/fixtures — run tools/record_run.py first.")

    def built_a_model(name: str) -> bool:
        for event in json.loads((FIXTURES / f"{name}.json").read_text()):
            if event.get("type") == "done":
                return bool((event.get("summary") or {}).get("completed"))
        return False

    # The first fixture is what a visitor sees on landing, so lead with a run that
    # produced a model. A refusal is worth showing, but not as the first impression.
    names.sort(key=lambda n: (not built_a_model(n), n))

    downloads = set()
    for name in names:
        events = json.loads((FIXTURES / name).with_suffix(".json").read_text())
        # (written after the cost pass below)
        for event in events:
            # The server normally prices usage events; a static page has no server, so
            # bake the same figure in at build time from the same rates.
            if event.get("type") == "usage":
                rate = PRICING.get(event.get("model", ""))
                if rate:
                    inp, out = rate
                    event["cost_usd"] = round(
                        event["input_tokens"] * inp / 1e6
                        + event.get("cache_read_tokens", 0) * inp * 0.1 / 1e6
                        + event.get("cache_write_tokens", 0) * inp * 1.25 / 1e6
                        + event["output_tokens"] * out / 1e6, 4)
            if event.get("type") == "done":
                target = (event.get("summary") or {}).get("download")
                if target:
                    downloads.add(target)

        (SITE / "fixtures" / f"{name}.json").write_text(json.dumps(events))

    for book in WORKBOOKS:
        if book.exists():
            shutil.copy2(book, SITE / "files" / book.name)
    # A fixture may name a workbook produced by a run whose file was never committed.
    # Fall back to the committed SHOE model so the download button is never a 404.
    fallback = SITE / "files" / "SHOE_LBO_Model.xlsx"
    for target in downloads:
        if fallback.exists() and not (SITE / "files" / target).exists():
            shutil.copy2(fallback, SITE / "files" / target)

    html = (WEBAPP / "index.html").read_text()
    anchor = "<script>\nconst $ = (id)"
    if anchor not in html:
        # Fail loudly. A missed anchor injects nothing, the page falls back to opening an
        # SSE connection that does not exist on a static host, and the build still
        # reports success — a silently broken site.
        raise SystemExit(
            "build_site: could not find the main <script> block in webapp/index.html. "
            "The anchor this build injects before has changed; update `anchor` here.")
    banner = ("<script>\n"
              "// Static build: no backend. The page replays recorded runs client-side.\n"
              f"window.PAPERLBO_STATIC = {json.dumps(names)};\n"
              "</script>\n")
    html = html.replace(anchor, banner + anchor, 1)
    if "window.PAPERLBO_STATIC" not in html.split("const STATIC")[0]:
        raise SystemExit("build_site: the static flag was injected after it is read.")
    (SITE / "index.html").write_text(html)
    (SITE / ".nojekyll").touch()   # keep Pages from filtering files it thinks are Jekyll

    return SITE


if __name__ == "__main__":
    out = build()
    files = sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file())
    total = sum(p.stat().st_size for p in out.rglob("*") if p.is_file())
    print(f"Built {out.relative_to(REPO)}/ — {len(files)} files, {total/1024:.0f} KB")
    for f in files:
        print(f"  {f}")
