"""HTTP + WebSocket layer.

Intentionally thin. Every route is a direct call to a Supervisor method - there
is no control logic here, so the UI cannot invent a new way to touch hardware.
There is no safety layer to speak for (see docs/CONTROL_MODEL.md); a command the
Supervisor cannot carry out right now - an unknown id, a sweep already running,
the Ar isolation-valve check - comes back as 409 with its reason text, which is
what the UI shows the operator.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import re
import secrets
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .data import DataFiles, create_data_router, merged_name
from .hcpes_analysis import HcpesAnalysisFiles, create_hcpes_analysis_router
from .. import instances
from ..config import ReactorConfig, load_config
from ..control.recipe import Recipe, build_ald_recipe, build_cvd_recipe
from ..control.hcpes_model import CURRENT_PLAN_ID, capability_catalog as hcpes_capabilities
from ..control.hcpes_store import LinkedSession
from ..supervisor import Supervisor
from ..dependencies import StatePaths, DeviceFactory
from ..aperture_lifetime import ApertureLifetimeConflict, ApertureLifetimeError

log = logging.getLogger("reactor.server")

STATIC = Path(__file__).parent / "static"
RECIPE_DIR = Path(__file__).resolve().parents[2] / "config" / "recipes"


class ReloadableStaticFiles(StaticFiles):
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


class BasicAuthMiddleware:
    """HTTP Basic auth over the whole app - pages, API, and the WebSocket - so
    reaching the server is not enough to touch the reactor. A pure ASGI
    middleware (not Starlette's http-only BaseHTTPMiddleware) because the live
    telemetry socket handshake must be guarded too.

    The credential comes from the environment (REACTOR_USER / REACTOR_PASSWORD),
    never the repo. This is a login, not encryption: run it behind the VPN
    (Tailscale) or LAN, where the transport is already private - Basic sends the
    password on each request, so the tunnel is what keeps it off the wire.
    """

    def __init__(self, app, username: str, password: str) -> None:
        self.app = app
        self.username = username
        self.password = password

    def _ok(self, header: str) -> bool:
        if not header.startswith("Basic "):
            return False
        try:
            user, _, pw = base64.b64decode(header[6:]).decode("utf-8").partition(":")
        except Exception:
            return False
        # constant-time compares, both sides, so neither field short-circuits
        return (secrets.compare_digest(user, self.username)
                & secrets.compare_digest(pw, self.password))

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        header = dict(scope.get("headers") or {}).get(b"authorization", b"").decode()
        if self._ok(header):
            return await self.app(scope, receive, send)
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})   # policy violation
            return
        body = b"Reactor interface: authentication required."
        await send({"type": "http.response.start", "status": 401, "headers": [
            (b"www-authenticate", b'Basic realm="Reactor Interface"'),
            (b"content-type", b"text/plain; charset=utf-8"),
            (b"content-length", str(len(body)).encode()),
        ]})
        await send({"type": "http.response.body", "body": body})

# A command that can't run right now (bad id, unknown device, "a sweep is already
# running") returns 409 with the reason, rather than a 500.
REFUSALS = (RuntimeError, KeyError, ValueError)


#: Run-tab parameters, shared by every browser that connects. See the
#: /api/run_params endpoints for why this is server-side.
RUN_PARAMS_PATH = (Path(__file__).resolve().parent.parent.parent
                   / "config" / "run_params.json")

#: The two process-gas MFCs used to be keyed by the gas on them ("h2_gas_pct").
#: The gas is selected on the unit and changes, so they are keyed by CHANNEL now
#: (2026-09-09) - but a saved run_params.json written before that still uses the
#: old names, as does a browser tab holding the old page, and losing an
#: operator's flows and percentages to a rename is not acceptable. Everything
#: that READS parameters goes through this; nothing writes the old keys back.
from ..control.parameters import migrate_params

#: Analysis-page plot layout - which plots, which columns, which ranges.
#: Server-owned for the same reason as RUN_PARAMS_PATH: one reactor, one set of
#: plots. It lived only in each browser's localStorage until 2026-08-26, so the
#: reactor PC and a laptop over Tailscale showed different grids.
ANALYSIS_LAYOUT_PATH = (Path(__file__).resolve().parent.parent.parent
                        / "config" / "analysis_layout.json")


def _kill_other_reactor_servers() -> list[int]:
    """Terminate every OTHER running reactor server. Returns the PIDs hit.

    Reads the instance registry (`reactor.instances`) rather than asking
    Windows, and terminates through the Win32 API rather than `taskkill`. Both
    changed on 2026-09-10 for one reason: SPEED. This runs inside the shutdown
    request, before the browser is told anything, and the old version shelled
    out to `powershell.exe` for a Win32_Process query - a 1-3 s cold start on
    this machine, every single press, which is most of what "20 s hold is way
    too long" was. The registry read is microseconds.

    The command line was the only thing that distinguished a reactor server
    from any other pythonw.exe and nothing in the standard library can read
    another process's command line on Windows, so the servers register
    themselves instead - see reactor/instances.py.

    Still blocking (it waits for each process to actually go), so call it off
    the event loop.
    """
    try:
        return instances.kill_others()
    except Exception as exc:
        log.warning("could not sweep other reactor servers: %s", exc)
        return []


def create_app(cfg: ReactorConfig | None = None, *, paths: StatePaths | None = None,
               devices: DeviceFactory | None = None, supervisor: Supervisor | None = None,
               shutdown_peers=None) -> FastAPI:
    cfg = cfg or (supervisor.cfg if supervisor is not None else load_config())
    sup = supervisor or Supervisor(cfg, paths=paths, devices=devices)
    isolated = paths is not None or supervisor is not None
    run_params_path = sup.paths.run_params if isolated else RUN_PARAMS_PATH
    analysis_layout_path = sup.paths.analysis_layout if isolated else ANALYSIS_LAYOUT_PATH
    kill_peers = shutdown_peers or (
        (lambda: instances.kill_others(directory=sup.paths.instances)) if isolated
        else _kill_other_reactor_servers)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        # The operator's saved Advanced-timing values for the Ar soft open, so
        # the first manual open after a restart uses them rather than the
        # built-in fallbacks (see Supervisor.set_soft_open_params).
        try:
            sup.set_soft_open_params(migrate_params(
                json.loads(run_params_path.read_text(encoding="utf-8"))))
        except Exception:
            pass            # no saved params yet - the defaults stand
        await sup.start()
        try:
            yield
        finally:
            await sup.stop()

    app = FastAPI(title="Reactor Interface", lifespan=lifespan)
    app.state.supervisor = sup
    app.include_router(create_data_router(DataFiles(sup.logger.dir)))
    app.include_router(create_hcpes_analysis_router(HcpesAnalysisFiles(sup.logger.dir)))

    # Optional login (see BasicAuthMiddleware). Off unless REACTOR_PASSWORD is
    # set, so localhost development is unchanged; set it before exposing the
    # server beyond localhost (Tailscale / LAN). The secret is read from the
    # environment - it is never stored in the repo or config.
    _user = os.environ.get("REACTOR_USER", "reactor")
    _password = os.environ.get("REACTOR_PASSWORD", "")
    if _password:
        app.add_middleware(BasicAuthMiddleware, username=_user, password=_password)
        log.info("auth: HTTP Basic login enabled (user %r)", _user)
    else:
        log.warning("auth: DISABLED - set REACTOR_PASSWORD to require a login "
                    "before exposing this server off localhost")

    # Registered as exception handlers rather than a decorator: a decorator would
    # hide each endpoint's signature from FastAPI's dependency injection and
    # break parameter parsing.
    async def _refusal(_: Request, exc: Exception) -> JSONResponse:
        detail = str(exc) or type(exc).__name__
        if isinstance(exc, KeyError):
            detail = f"unknown device or id: {detail}"
        return JSONResponse(status_code=409, content={"detail": detail})

    for _exc_type in REFUSALS:
        app.add_exception_handler(_exc_type, _refusal)

    # -- pages ----------------------------------------------------------- #

    @app.get("/")
    async def index():
        # no-cache so a reload always picks up the latest UI (the file is small
        # and served locally, so revalidating every load costs nothing).
        return FileResponse(STATIC / "index.html",
                            headers={"Cache-Control": "no-cache"})

    @app.get("/analysis")
    async def analysis_page():
        """Post-run plotting. A separate page, not a tab, on purpose: it reads
        finished CSVs and can touch no hardware, so it stays out of the control
        UI entirely and can be opened alongside a running experiment."""
        return FileResponse(STATIC / "analysis.html",
                            headers={"Cache-Control": "no-cache"})

    if STATIC.exists():
        app.mount("/static", ReloadableStaticFiles(directory=STATIC), name="static")

    # -- data files (read-only; feeds the analysis page) ------------------- #

    # -- state ----------------------------------------------------------- #

    @app.get("/api/state")
    async def get_state() -> dict[str, Any]:
        return sup.state()

    @app.post("/api/aperture/replaced")
    async def replace_aperture(
        expected_aperture_id: str = Body(..., embed=True),
        confirm: bool = Body(default=False, embed=True),
    ) -> dict[str, Any]:
        """Archive one aperture record.  This is bookkeeping, not a command."""
        if confirm is not True:
            raise HTTPException(
                status_code=400,
                detail="confirm New Aperture Installed before replacing the record",
            )
        before = sup.aperture_lifetime.snapshot()
        was_current = (before.get("available")
                       and before.get("current", {}).get("id")
                       == expected_aperture_id)
        try:
            state = sup.aperture_lifetime.replace(expected_aperture_id)
        except ApertureLifetimeConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ApertureLifetimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if was_current:
            sup.report_event(
                "maintenance",
                "new HCPES aperture installed; prior lifetime archived",
            )
        return {"ok": True, "aperture_lifetime": state}

    @app.get("/api/events")
    async def get_events(limit: int = 20000) -> dict[str, Any]:
        """The event scrollback, newest `limit` entries.

        The live telemetry frame carries only the last 200 - see Supervisor's
        state() for why - so the browser seeds its log from here once and then
        appends. The default is a seed size, not the buffer size: the buffer
        holds 200 000 and shipping all of them at once would be tens of MB over
        Tailscale. Pass `limit` to reach further back. Every event is also in
        server.log permanently, and a run's own events are in its folder; this
        is the in-memory copy, and it starts empty after a restart.
        """
        return {"events": list(sup.events)[-limit:]}

    @app.get("/api/errors")
    async def get_errors(limit: int = 20000) -> dict[str, Any]:
        """The error scrollback: the same entries as /api/events, filtered to
        the kinds worth finding without scrolling (errors and flags).

        Its own endpoint rather than a query parameter on /api/events because
        the browser keeps the two panels separately seeded and tailed, and
        because a run writes the same split to disk (`*_errors.log`).
        """
        return {"errors": list(sup.errors)[-limit:]}

    @app.get("/api/trend")
    async def get_trend(limit: int = 1800) -> dict[str, Any]:
        return {"samples": sup.trend(limit)}

    @app.get("/api/config")
    async def get_config() -> dict[str, Any]:
        return {
            "site": cfg.site.model_dump(mode="json"),
            "pressure": cfg.pressure.model_dump(mode="json"),
            "gauges": [g.model_dump(mode="json") for g in cfg.gauges],
            "stage_temp": cfg.stage_temp.model_dump(mode="json"),
            "aux_inputs": [a.model_dump(mode="json") for a in cfg.aux_inputs],
            "valve_banks": [b.model_dump(mode="json") for b in cfg.valve_banks],
            "valves": [v.model_dump(mode="json") for v in cfg.valves],
            "mfcs": [m.model_dump(mode="json") for m in cfg.mfcs],
            "logging": cfg.logging.model_dump(mode="json"),
        }

    @app.websocket("/ws")
    async def ws(sock: WebSocket) -> None:
        await sock.accept()
        q = sup.subscribe()
        try:
            await sock.send_json(sup.state())
            while True:
                await sock.send_json(await q.get())
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        except Exception:
            pass
        finally:
            sup.unsubscribe(q)

    # -- valves ---------------------------------------------------------- #

    @app.post("/api/valve/{valve_id}")
    async def set_valve(valve_id: str, state: bool = Body(..., embed=True)) -> dict[str, Any]:
        await sup.set_valve(valve_id, state, reason="manual")
        return {"id": valve_id, "open": state}

    @app.post("/api/label")
    async def set_label(
        kind: str = Body(..., embed=True),
        id: str = Body(..., embed=True),
        label: str = Body(default="", embed=True),
    ) -> dict[str, Any]:
        value = sup.set_label(kind, id, label)
        return {"kind": kind, "id": id, "label": value}

    # -- valve identification -------------------------------------------- #

    @app.post("/api/valve_id/start")
    async def valve_id_start(
        lines: list[str] | None = Body(default=None, embed=True),
        group: str | None = Body(default=None, embed=True),
        reps: int = Body(default=3, embed=True),
        on_s: float = Body(default=1.0, embed=True),
        off_s: float = Body(default=1.0, embed=True),
        gap_s: float = Body(default=3.0, embed=True),
        start_index: int = Body(default=0, embed=True),
    ) -> dict[str, Any]:
        from ..supervisor import DO_LINE_GROUPS

        chosen = list(lines) if lines else DO_LINE_GROUPS.get(group or "", [])
        if not chosen:
            raise HTTPException(400, "provide 'lines' or a known 'group'")
        await sup.start_valve_sweep(
            chosen, reps=reps, on_s=on_s, off_s=off_s, gap_s=gap_s,
            start_index=start_index,
        )
        return {"running": True, "total": len(chosen)}

    @app.post("/api/valve_id/stop")
    async def valve_id_stop() -> dict[str, Any]:
        await sup.stop_valve_sweep()
        return {"running": False}

    @app.post("/api/valve_id/mark")
    async def valve_id_mark(
        valve: str = Body(default="", embed=True),
        note: str = Body(default="", embed=True),
    ) -> dict[str, Any]:
        return sup.mark_sweep_line(valve, note)

    # -- MFCs ------------------------------------------------------------ #

    @app.post("/api/mfc/{mfc_id}/setpoint")
    async def set_mfc(mfc_id: str, sccm: float = Body(..., embed=True)) -> dict[str, Any]:
        value = await sup.set_mfc_setpoint(mfc_id, sccm)
        return {"id": mfc_id, "setpoint_sccm": value}

    # -- run parameters, owned by the server ------------------------------- #
    #
    # These used to live only in each browser's localStorage, so every machine
    # had its own copy: opening the UI over Tailscale from a laptop showed
    # default cycles/dose/bias rather than what the reactor PC had set
    # (reported 2026-08-25). One reactor should present one set of parameters,
    # so the server holds them - the same reasoning that already makes the run
    # NAME server-owned (see RUN_NAME_PATH in supervisor.py).
    #
    # The browser still keeps a localStorage copy as a cache, so the fields are
    # populated instantly on load and survive the server being unreachable.
    # Last write wins if two browsers edit at once; this is a single-operator
    # tool and that is fine.

    @app.get("/api/run_params")
    async def get_run_params() -> dict[str, Any]:
        try:
            return {"params": migrate_params(json.loads(
                run_params_path.read_text(encoding="utf-8")))}
        except Exception:
            # No file yet, or it is unreadable - the UI falls back to its own
            # cache and then to the field defaults.
            return {"params": {}}

    @app.post("/api/run_params")
    async def set_run_params(params: dict = Body(default={})) -> dict[str, Any]:
        params = migrate_params(params)
        run_params_path.parent.mkdir(parents=True, exist_ok=True)
        run_params_path.write_text(
            json.dumps(params, indent=2, sort_keys=True), encoding="utf-8")
        # Most of these are only read when a run starts. The Ar soft-open pulse
        # settings are the exception: they also govern a MANUAL open from the
        # Hardware tab, which carries no run parameters at all, so the
        # supervisor is handed them as they are saved.
        sup.set_soft_open_params(params)
        return {"params": params}

    # The Analysis page's plot grid, same ownership model as the run params
    # above: the server holds it, each browser keeps a localStorage copy as a
    # cache so the page paints instantly and still works if the fetch fails.
    # The dropped Auger spectra are deliberately NOT here - those are data a
    # person dropped on one machine, not layout, and can be megabytes.

    @app.get("/api/analysis_layout")
    async def get_analysis_layout() -> dict[str, Any]:
        try:
            return {"layout": json.loads(
                analysis_layout_path.read_text(encoding="utf-8"))}
        except Exception:
            return {"layout": None}     # never saved, or unreadable

    @app.post("/api/analysis_layout")
    async def set_analysis_layout(layout: dict = Body(default={})) -> dict[str, Any]:
        analysis_layout_path.parent.mkdir(parents=True, exist_ok=True)
        analysis_layout_path.write_text(
            json.dumps(layout, indent=2), encoding="utf-8")
        return {"ok": True}

    # -- server shutdown --------------------------------------------------- #

    @app.post("/api/server/shutdown")
    async def shutdown_server() -> dict[str, Any]:
        """Stop this server, and any other reactor server still running.

        Replaces the Restart button (2026-08-25). Restarting re-exec'd the
        process, and on 2026-08-25 the OLD instance survived it: two servers
        were then up at once, the newer one holding port 8000 while the older
        one still held COM8-COM12, so the devices looked unreachable. Zach's
        call: "just a button that kills all servers in use", then start it again
        from the shortcut.

        Order matters, and it changed on 2026-09-10. It is now:

            1. terminate the other instances,
            2. run THIS server's device teardown, right here,
            3. answer with a receipt of what was released,
            4. and only then end the process.

        The teardown used to happen after this response, on the way out through
        the lifespan - which meant the only thing the page could observe was
        port 8000 going quiet, and that happens BEFORE the teardown: uvicorn
        drains the listening socket first. So the page reported success on the
        one fact that was never in question, while the DAQ and COM8-COM12 were
        still held. Zach, 2026-09-10: "there is no way for me to know if it
        worked or not. I need some confirmation things are shut down and ready
        to be booted again."

        Doing it here costs nothing - the teardown is a handful of disconnects,
        each separately bounded (Supervisor._teardown) - and buys a receipt
        delivered while there is still a connection to deliver it on.
        `Supervisor.stop` is idempotent, so the lifespan calling it again on the
        way out is a no-op.

        Same consequences as any stop: a running recipe is aborted with its full
        teardown, and gas stops because the MFCs zero their own setpoints when
        this program disconnects. The caller is expected to have confirmed that.
        """
        request_shutdown = getattr(app.state, "request_shutdown", None)
        if request_shutdown is None:
            raise HTTPException(
                status_code=501,
                detail="This server was not started with `python -m reactor`, "
                       "so it cannot shut itself down. Stop it by hand.")

        task = getattr(app.state, "shutdown_receipt_task", None)
        if task is None:
            task = asyncio.create_task(_shutdown_once(request_shutdown))
            app.state.shutdown_receipt_task = task
        return await asyncio.shield(task)

    async def _shutdown_once(request_shutdown):
        killed = await asyncio.to_thread(kill_peers)
        busy = sup.recipes.busy
        sup._event("command",
                   "server shutdown requested from the UI"
                   + (" DURING A RUN - the run will be aborted" if busy else "")
                   + (f"; also killed {len(killed)} other instance(s): {killed}"
                      if killed else ""))
        log.warning("shutdown requested from the UI (run active: %s, "
                    "other instances killed: %s)", busy, killed or "none")

        # The teardown, now, while the browser is still connected to hear how
        # it went. Bounded from the inside - every step in Supervisor.stop has
        # its own deadline - so this cannot park the response indefinitely.
        receipt = await sup.stop()
        log.warning("shutdown: released %s in %.2fs%s",
                    ", ".join(receipt["released"]) or "nothing",
                    receipt["elapsed_s"],
                    "" if receipt["ok"] else
                    f" ({len(receipt['failed'])} step(s) failed)")

        async def _go() -> None:
            # Let this response reach the browser before the socket closes.
            await asyncio.sleep(0.25)
            request_shutdown()

        # Held on app.state, not fire-and-forget: the event loop keeps only a
        # WEAK reference to a task, so a bare create_task() can be collected
        # mid-sleep and the shutdown would then simply never happen.
        app.state.shutdown_task = asyncio.create_task(_go())
        return {"stopping": True, "run_was_active": busy, "also_killed": killed,
                **receipt}

    # -- power supplies -------------------------------------------------- #
    #
    # Operator-requested 2026-08-25: voltage/current fields and an output
    # toggle per supply on the Hardware tab. These are the ONLY paths from the
    # browser to a supply output, and they exist for the Keithley 2260B DC
    # supplies. The Glassman has no route here at all - it has no set path in
    # its driver beyond hv_off, by design (docs/CONTROL_MODEL.md).

    @app.post("/api/supply/{supply_id}/voltage")
    async def set_supply_voltage(
        supply_id: str, volts: float = Body(..., embed=True)
    ) -> dict[str, Any]:
        return await sup.set_supply_voltage(supply_id, volts)

    @app.post("/api/supply/{supply_id}/current")
    async def set_supply_current(
        supply_id: str, amps: float = Body(..., embed=True)
    ) -> dict[str, Any]:
        return await sup.set_supply_current(supply_id, amps)

    @app.post("/api/supply/{supply_id}/output")
    async def set_supply_output(
        supply_id: str, on: bool = Body(..., embed=True)
    ) -> dict[str, Any]:
        return await sup.set_supply_output(supply_id, on)

    # -- fill regulation (standalone, e.g. to charge precursor before a run) -- #

    @app.post("/api/fill/start")
    async def fill_start(
        valve: str = Body(..., embed=True),
        gauge: str = Body(..., embed=True),
        target_torr: float = Body(..., embed=True),
        pulse_on_s: float = Body(default=0.1, embed=True),
        pulse_off_s: float = Body(default=0.3, embed=True),
        tolerance_frac: float = Body(default=0.2, embed=True),
    ) -> dict[str, Any]:
        await sup.start_fill_regulation(
            valve=valve, gauge=gauge, target_torr=target_torr,
            pulse_on_s=pulse_on_s, pulse_off_s=pulse_off_s,
            tolerance_frac=tolerance_frac,
        )
        return {"running": True}

    @app.post("/api/fill/stop")
    async def fill_stop() -> dict[str, Any]:
        await sup.stop_fill_regulation()
        return {"running": False}

    # -- recipes --------------------------------------------------------- #

    @app.get("/api/recipes")
    async def list_recipes() -> dict[str, Any]:
        if not RECIPE_DIR.exists():
            return {"recipes": []}
        out = []
        for p in sorted(RECIPE_DIR.glob("*.y*ml")):
            try:
                r = Recipe.load(p)
                out.append({
                    "file": p.name, "name": r.name, "cycles": r.cycles,
                    "steps": len(r.steps), "cycle_seconds": r.cycle_seconds(),
                    "notes": r.notes,
                })
            except Exception as exc:
                out.append({"file": p.name, "name": p.stem, "error": str(exc)})
        return {"recipes": out}

    @app.get("/api/run/next-name")
    async def next_run_name() -> dict[str, Any]:
        """Name to pre-fill for the next run: the last STARTED run's name with
        its trailing number incremented (Mo-014 -> Mo-015)."""
        return {"suggested": sup.suggest_run_name(), "last": sup.last_run_name}

    @app.post("/api/run/ald")
    async def start_ald(params: dict = Body(...)) -> dict[str, Any]:
        recipe = await sup.start_ald_run(migrate_params(params))
        return {"started": recipe.name, "cycles": recipe.cycles}

    @app.post("/api/run/cvd")
    async def start_cvd(params: dict = Body(...)) -> dict[str, Any]:
        recipe = await sup.start_cvd_run(migrate_params(params))
        return {"started": recipe.name, "cycles": recipe.cycles}

    @app.post("/api/run/params")
    async def update_run_params(params: dict = Body(default={})) -> dict[str, Any]:
        """Change parameters on the run in progress (2026-09-01).

        Same body as /api/run/ald and /api/run/cvd - the browser posts what it
        would start a run with, and the supervisor diffs it against what the run
        is actually using, so only what moved is applied and logged. 409 if no
        run is in progress; the Run tab's fields are then just the next run's
        settings, saved through /api/run_params as before.
        """
        try:
            return await sup.update_run_params(migrate_params(params or {}))
        except REFUSALS as exc:
            raise HTTPException(409, str(exc))

    @app.post("/api/run/estimate")
    async def estimate_run(params: dict = Body(default={})) -> dict[str, Any]:
        """How long a run with these parameters would take. Starts nothing.

        This is what the Run tab shows between runs, so that "how long is 150
        cycles of this?" is answerable from the parameters as typed rather than
        by starting a run. It builds the SAME recipe the run would build and
        reports its length, which is by construction the number the countdown
        starts from (RecipeRunner.run_total_s: cycle length x cycles, setup and
        teardown excluded). A browser-side copy of that arithmetic is exactly
        what the countdown itself was moved off in 2026-08-21.

        A build failure is not an error here - the operator is typing, and a
        half-edited gas schedule (one lone "simultaneous") raises. Answer 200
        with total_s: null and let the panel show a dash.
        """
        build = (build_cvd_recipe
                 if str(params.get("mode", "ald")).lower() == "cvd"
                 else build_ald_recipe)
        try:
            recipe = build(migrate_params(params or {}))
        except Exception as exc:
            return {"total_s": None, "error": str(exc)}
        cycle_s = recipe.cycle_seconds()
        return {"cycle_s": cycle_s, "cycles": recipe.cycles,
                "total_s": cycle_s * recipe.cycles}

    @app.post("/api/prestart/start")
    async def prestart_start(params: dict = Body(default={})) -> dict[str, Any]:
        await sup.start_prestart(params or {})
        return sup.prestart

    @app.get("/api/prestart/capabilities")
    async def prestart_capabilities() -> dict[str, Any]:
        """Device/action schema used by validation, execution, and the editor."""
        return sup.prestart_recipes.catalog

    @app.get("/api/prestart/recipes")
    async def prestart_recipes() -> dict[str, Any]:
        return sup.prestart_recipes.payload()

    @app.post("/api/prestart/recipes")
    async def prestart_recipe_create(payload: dict = Body(default={})) -> dict[str, Any]:
        recipe = sup.prestart_recipes.create(
            str(payload.get("name") or "Untitled pre-start"),
            from_id=str(payload.get("from_id") or "current-prestart"))
        return recipe.model_dump(mode="json")

    @app.put("/api/prestart/recipes/{recipe_id}")
    async def prestart_recipe_save(
        recipe_id: str, payload: dict = Body(...),
    ) -> dict[str, Any]:
        if "recipe" not in payload or "expected_revision" not in payload:
            raise ValueError("save requires recipe and expected_revision")
        recipe = sup.prestart_recipes.save(
            recipe_id, payload["recipe"],
            expected_revision=int(payload["expected_revision"]))
        return recipe.model_dump(mode="json")

    @app.delete("/api/prestart/recipes/{recipe_id}")
    async def prestart_recipe_delete(recipe_id: str) -> dict[str, Any]:
        if sup.prestart.get("cleanup_available") and (
                sup.prestart.get("recipe_id") == recipe_id):
            raise RuntimeError("cannot delete the active or primed pre-start recipe")
        return sup.prestart_recipes.delete(recipe_id).model_dump(mode="json")

    @app.post("/api/prestart/recipes/{recipe_id}/select")
    async def prestart_recipe_select(recipe_id: str) -> dict[str, Any]:
        library = sup.prestart_recipes.select(recipe_id)
        return {"selected_id": library.selected_id}

    @app.post("/api/prestart/preview")
    async def prestart_preview(payload: dict = Body(default={})) -> dict[str, Any]:
        return sup.prestart_recipes.preview(
            str(payload.get("recipe_id")) if payload.get("recipe_id") else None,
            dict(payload.get("values") or {}))

    @app.post("/api/prestart/abort")
    async def prestart_abort() -> dict[str, Any]:
        """Undo the pre-start in one call: Ar off, fill off, beam relay at rest,
        HV off. The only way out of a pre-start, running or already struck: it
        calls stop_prestart itself and then undoes what the sequence turned on.
        A /api/prestart/stop route used to end just the sequence, leaving the
        tool primed; it was dropped with its button (2026-08-28)."""
        await sup.abort_prestart()
        return sup.prestart

    # -- HCPES hollow-cathode characterization -------------------------- #

    @app.get("/api/hcpes/capabilities")
    async def hcpes_capability_catalog() -> dict[str, Any]:
        """Typed targets shared by plan validation and the visual editor."""
        return hcpes_capabilities(cfg)

    @app.get("/api/hcpes/plans")
    async def hcpes_plans() -> dict[str, Any]:
        return sup.hcpes_plans.payload()

    @app.post("/api/hcpes/plans")
    async def hcpes_plan_create(payload: dict = Body(default={})) -> dict[str, Any]:
        plan = sup.hcpes_plans.create(
            str(payload.get("name") or "Untitled HCPES plan"),
            from_id=str(payload.get("from_id") or CURRENT_PLAN_ID),
        )
        return plan.model_dump(mode="json")

    @app.put("/api/hcpes/plans/{plan_id}")
    async def hcpes_plan_save(
        plan_id: str, payload: dict = Body(...),
    ) -> dict[str, Any]:
        if "plan" not in payload or "expected_revision" not in payload:
            raise ValueError("save requires plan and expected_revision")
        plan = sup.hcpes_plans.save(
            plan_id,
            payload["plan"],
            expected_revision=int(payload["expected_revision"]),
        )
        return plan.model_dump(mode="json")

    @app.delete("/api/hcpes/plans/{plan_id}")
    async def hcpes_plan_delete(plan_id: str) -> dict[str, Any]:
        if sup.hcpes_running and sup.hcpes.get("plan_id") == plan_id:
            raise RuntimeError("cannot delete the active HCPES plan")
        return sup.hcpes_plans.delete(plan_id).model_dump(mode="json")

    @app.post("/api/hcpes/plans/{plan_id}/select")
    async def hcpes_plan_select(plan_id: str) -> dict[str, Any]:
        library = sup.hcpes_plans.select(plan_id)
        return {"selected_id": library.selected_id}

    @app.post("/api/hcpes/preview")
    async def hcpes_preview(payload: dict = Body(default={})) -> dict[str, Any]:
        plan_id = str(payload.get("plan_id")) if payload.get("plan_id") else None
        return sup.hcpes_plans.preview(plan_id, payload.get("plan"))

    @app.post("/api/hcpes/start")
    async def hcpes_start(payload: dict = Body(...)) -> dict[str, Any]:
        required = {"plan_id", "expected_revision", "session_id", "polarity_confirmed"}
        missing = sorted(required - set(payload))
        if missing:
            raise ValueError(f"HCPES start is missing: {', '.join(missing)}")
        session_id = str(payload["session_id"])
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", session_id) is None:
            raise ValueError(
                "HCPES session id must use 1-80 letters, digits, dot, dash, or underscore")
        if payload["polarity_confirmed"] is not True:
            raise RuntimeError("confirm the physical stage-lead polarity before starting")
        resolved = sup.hcpes_plans.resolve_saved(
            str(payload["plan_id"]), int(payload["expected_revision"]))
        if resolved.plan.builtin:
            raise RuntimeError("duplicate and save the protected HCPES template before starting")
        campaign_id = (
            str(payload["campaign_id"]) if payload.get("campaign_id") else None)
        await sup.start_hcpes(
            resolved,
            session_id,
            polarity_confirmed=True,
            campaign_id=campaign_id,
        )
        return sup.hcpes

    @app.post("/api/hcpes/stop")
    async def hcpes_stop() -> dict[str, Any]:
        await sup.stop_hcpes()
        return sup.hcpes

    @app.post("/api/hcpes/plans/{plan_id}/opposite")
    async def hcpes_opposite_plan(
        plan_id: str, payload: dict = Body(...),
    ) -> dict[str, Any]:
        if "expected_revision" not in payload or "source_session_id" not in payload:
            raise ValueError(
                "opposite-polarity clone requires expected_revision and source_session_id")
        source_session_id = str(payload["source_session_id"])
        hcpes_state = sup.hcpes
        recording_state = sup.recording.status().get("hcpes", {})
        if (hcpes_state.get("session_id") != source_session_id
                or hcpes_state.get("phase") != "complete"
                or hcpes_state.get("plan_id") != plan_id
                or recording_state.get("session_id") != source_session_id
                or recording_state.get("status") != "complete"):
            raise RuntimeError(
                "the source must be the most recently completed HCPES session")
        directory = Path(str(recording_state.get("directory", ""))).resolve()
        if directory.parent != sup.logger.dir.resolve():
            raise RuntimeError("completed HCPES session is outside the configured data directory")
        source = sup.hcpes_plans.get(plan_id)
        source_session = LinkedSession(
            session_id=source_session_id,
            plan_id=plan_id,
            polarity=source.stage_polarity,
            directory_name=directory.name,
        )
        opposite, campaign = sup.hcpes_plans.create_opposite(
            plan_id,
            expected_revision=int(payload["expected_revision"]),
            name=str(payload.get("name") or f"{source.name} opposite polarity"),
            campaign_name=str(payload.get("campaign_name") or f"{source.name} polarities"),
            source_session=source_session,
            source_signature=str(hcpes_state.get("plan_signature") or ""),
        )
        return {
            "plan": opposite.model_dump(mode="json"),
            "campaign": campaign.model_dump(mode="json"),
        }

    @app.post("/api/recipe/start")
    async def start_recipe(file: str = Body(..., embed=True)) -> dict[str, Any]:
        path = (RECIPE_DIR / file).resolve()
        if RECIPE_DIR.resolve() not in path.parents or not path.exists():
            raise HTTPException(404, f"no such recipe: {file}")
        recipe = Recipe.load(path)
        await sup.start_recipe(recipe)
        return {"started": recipe.name, "cycles": recipe.cycles}

    @app.post("/api/recipe/pause")
    async def pause_recipe() -> dict[str, Any]:
        sup.recipes.pause()
        return {"state": sup.recipes.progress.state}

    @app.post("/api/recipe/resume")
    async def resume_recipe() -> dict[str, Any]:
        sup.recipes.resume()
        return {"state": sup.recipes.progress.state}

    @app.post("/api/recipe/abort")
    async def abort_recipe() -> dict[str, Any]:
        await sup.abort_recipe()
        return {"state": sup.recipes.progress.state}

    # -- logging --------------------------------------------------------- #

    @app.post("/api/log/start")
    async def log_start(label: str | None = Body(default=None, embed=True)) -> dict[str, Any]:
        path = await sup.recording.start_manual_log(label)
        sup._event("log", f"logging to {path.name}")
        return sup.recording.status()

    @app.post("/api/log/stop")
    async def log_stop() -> dict[str, Any]:
        await sup.recording.stop_manual_log()
        sup._event("log", "logging stopped")
        return sup.recording.status()

    # -- ellipsometer sync ----------------------------------------------- #
    #  Post-run: put a *refit* FS-1 dynamic file back onto the reactor clock,
    #  using the (fs_time -> reactor_clock) sidecar captured live during the
    #  run. Read-only file handling; no hardware.

    return app
