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
import secrets
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .data import DataFiles, create_data_router, merged_name
from ..config import ReactorConfig, load_config
from ..control.recipe import Recipe
from ..supervisor import Supervisor

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


def _kill_other_reactor_servers() -> list[int]:
    """Terminate every OTHER `python -m reactor` process. Returns the PIDs hit.

    Blocking (shells out to PowerShell); call it off the event loop. This
    program's own PID and its parent are skipped: the parent is the venv
    launcher shim that spawned us, and killing it before our own teardown would
    take the console down early.
    """
    import subprocess

    me, parent = os.getpid(), os.getppid()
    ps = ("Get-CimInstance Win32_Process | Where-Object { "
          "$_.CommandLine -like '*-m reactor*' -and $_.Name -match '^python' "
          "} | ForEach-Object { $_.ProcessId }")
    try:
        out = subprocess.run(["powershell.exe", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, timeout=25).stdout
    except Exception as exc:
        log.warning("could not enumerate reactor processes: %s", exc)
        return []

    killed: list[int] = []
    for tok in out.split():
        try:
            pid = int(tok)
        except ValueError:
            continue
        if pid in (me, parent):
            continue
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=15)
            killed.append(pid)
        except Exception as exc:
            log.warning("could not kill reactor PID %s: %s", pid, exc)
    return killed


def create_app(cfg: ReactorConfig | None = None) -> FastAPI:
    cfg = cfg or load_config()
    sup = Supervisor(cfg)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        await sup.start()
        try:
            yield
        finally:
            await sup.stop()

    app = FastAPI(title="Reactor Interface", lifespan=lifespan)
    app.state.supervisor = sup
    app.include_router(create_data_router(DataFiles(sup.logger.dir)))

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

    @app.get("/api/events")
    async def get_events(limit: int = 20000) -> dict[str, Any]:
        """The full event scrollback.

        The live telemetry frame carries only the last 200 - see Supervisor's
        state() for why - so the browser seeds its log from here once and then
        appends. Every event is also in server.log permanently; this is the
        in-memory copy, and it starts empty after a restart.
        """
        return {"events": list(sup.events)[-limit:]}

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
            return {"params": json.loads(
                RUN_PARAMS_PATH.read_text(encoding="utf-8"))}
        except Exception:
            # No file yet, or it is unreadable - the UI falls back to its own
            # cache and then to the field defaults.
            return {"params": {}}

    @app.post("/api/run_params")
    async def set_run_params(params: dict = Body(default={})) -> dict[str, Any]:
        RUN_PARAMS_PATH.parent.mkdir(parents=True, exist_ok=True)
        RUN_PARAMS_PATH.write_text(
            json.dumps(params, indent=2, sort_keys=True), encoding="utf-8")
        return {"params": params}

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

        Order matters. Other instances are terminated FIRST, then this one shuts
        down gracefully through the normal lifespan teardown - so the server you
        are talking to releases its devices properly, and any orphan holding a
        serial port is gone by the time you restart.

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

        killed = await asyncio.to_thread(_kill_other_reactor_servers)
        busy = sup.recipes.busy
        sup._event("command",
                   "server shutdown requested from the UI"
                   + (" DURING A RUN - the run will be aborted" if busy else "")
                   + (f"; also killed {len(killed)} other instance(s): {killed}"
                      if killed else ""))
        log.warning("shutdown requested from the UI (run active: %s, "
                    "other instances killed: %s)", busy, killed or "none")

        async def _go() -> None:
            # Let this response reach the browser before the socket closes.
            await asyncio.sleep(0.25)
            request_shutdown()

        asyncio.create_task(_go())
        return {"stopping": True, "run_was_active": busy, "also_killed": killed}

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
        recipe = await sup.start_ald_run(params)
        return {"started": recipe.name, "cycles": recipe.cycles}

    @app.post("/api/run/cvd")
    async def start_cvd(params: dict = Body(...)) -> dict[str, Any]:
        recipe = await sup.start_cvd_run(params)
        return {"started": recipe.name, "cycles": recipe.cycles}

    @app.post("/api/prestart/start")
    async def prestart_start(params: dict = Body(default={})) -> dict[str, Any]:
        await sup.start_prestart(params or {})
        return sup.prestart

    @app.post("/api/prestart/stop")
    async def prestart_stop() -> dict[str, Any]:
        await sup.stop_prestart()
        return sup.prestart

    @app.post("/api/prestart/abort")
    async def prestart_abort() -> dict[str, Any]:
        """Undo the pre-start in one call: Ar off, fill off, beam relay at rest,
        HV off. Unlike /stop this stays available after the sequence has
        finished - a struck, primed tool is the state the operator most often
        needs to back out of, and until now nothing in the UI did it."""
        await sup.abort_prestart()
        return sup.prestart

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
        path = await sup.recording.call("start", label)
        sup._event("log", f"logging to {path.name}")
        return sup.recording.status()

    @app.post("/api/log/stop")
    async def log_stop() -> dict[str, Any]:
        await sup.recording.call("stop")
        sup._event("log", "logging stopped")
        return sup.recording.status()

    # -- ellipsometer sync ----------------------------------------------- #
    #  Post-run: put a *refit* FS-1 dynamic file back onto the reactor clock,
    #  using the (fs_time -> reactor_clock) sidecar captured live during the
    #  run. Read-only file handling; no hardware.

    return app
