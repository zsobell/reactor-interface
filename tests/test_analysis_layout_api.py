"""The Analysis page's plot layout is owned by the SERVER, not the browser.

Zach, 2026-08-26: "I am also not seeing the same plots on the analysis page on
every machine. Make sure those are conserved across machines like the rest of
the parameters." The layout lived only in each browser's localStorage, so the
reactor PC and a laptop over Tailscale kept separate grids - the same problem
the Run tab's parameters had, fixed the same way (config/analysis_layout.json
behind /api/analysis_layout, with localStorage demoted to a cache).

This drives the real FastAPI app over raw ASGI, via _support.asgi_call - no
lifespan, so nothing here goes near the DAQ or the serial ports of the server
actually running the reactor.

Run directly: python -m tests.test_analysis_layout_api
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, ".")

from reactor.server import app as app_mod
from tests._support import Checker, asgi_call as call


LAYOUT = {
    "cols": 3, "plotH": 300, "settings": True,
    "plots": [{"x": "cycle_number", "y": ["thickness_a"]},
              {"x": "cycle_number", "y": ["psu_stage_bias_voltage"]}],
}


async def main() -> int:
    c = Checker("test_analysis_layout_api")

    tmp = Path(tempfile.mkdtemp(prefix="analysis_layout_"))
    original = app_mod.ANALYSIS_LAYOUT_PATH
    app_mod.ANALYSIS_LAYOUT_PATH = tmp / "analysis_layout.json"
    try:
        app = app_mod.create_app()

        c.section("1. nothing saved yet")
        st, body = await call(app, "GET", "/api/analysis_layout")
        # None, not {} or a 404: the page has to be able to tell "no layout
        # has ever been saved" from "here is an empty one", or a first visit
        # would wipe the field defaults instead of seeding from them.
        c.check("200 with layout=None", st == 200 and body == {"layout": None},
                f"{st} {body}")

        c.section("2. round trip")
        st, body = await call(app, "POST", "/api/analysis_layout", LAYOUT)
        c.check("POST accepted", st == 200 and body == {"ok": True}, f"{st} {body}")
        st, body = await call(app, "GET", "/api/analysis_layout")
        c.check("GET returns exactly what was posted",
                st == 200 and body.get("layout") == LAYOUT, f"{st} {body}")

        on_disk = json.loads(
            app_mod.ANALYSIS_LAYOUT_PATH.read_text(encoding="utf-8"))
        c.check("the file on disk IS the layout, indented and readable",
                on_disk == LAYOUT, str(on_disk)[:100])

        c.section("3. a second browser overwrites it - last write wins")
        other = dict(LAYOUT, cols=1, plots=[{"x": "elapsed_s", "y": ["pressure"]}])
        await call(app, "POST", "/api/analysis_layout", other)
        st, body = await call(app, "GET", "/api/analysis_layout")
        c.check("the newer layout is what everyone gets",
                body.get("layout") == other, str(body)[:100])

        c.section("4. a damaged file must not 500 the page")
        # The browser cache is the fallback, and it only gets its chance if the
        # request succeeds with an empty answer.
        app_mod.ANALYSIS_LAYOUT_PATH.write_text("{not json", encoding="utf-8")
        st, body = await call(app, "GET", "/api/analysis_layout")
        c.check("degrades to layout=None", st == 200 and body == {"layout": None},
                f"{st} {body}")

        c.section("5. the run params it is modelled on still work")
        st, body = await call(app, "GET", "/api/run_params")
        c.check("GET /api/run_params still answers", st == 200 and "params" in body,
                f"{st} {str(body)[:60]}")
    finally:
        app_mod.ANALYSIS_LAYOUT_PATH = original

    return c.summary()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
