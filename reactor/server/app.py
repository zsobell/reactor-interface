"""HTTP + WebSocket layer.

Intentionally thin. Every route is a direct call to a Supervisor method - there
is no control logic here, so the UI cannot invent a new way to touch hardware.
Refusals from the safety layer come back as 409 with the reason text, which is
what the UI shows the operator.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import os
import secrets
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..analysis import ellipsometer_merge as ell
from ..config import ReactorConfig, load_config
from ..control.recipe import Recipe
from ..supervisor import Supervisor

log = logging.getLogger("reactor.server")

STATIC = Path(__file__).parent / "static"
RECIPE_DIR = Path(__file__).resolve().parents[2] / "config" / "recipes"


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

    if STATIC.exists():
        app.mount("/static", StaticFiles(directory=STATIC), name="static")

    # -- state ----------------------------------------------------------- #

    @app.get("/api/state")
    async def get_state() -> dict[str, Any]:
        return sup.state()

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
        path = sup.logger.start(label)
        sup._event("log", f"logging to {path.name}")
        return sup.logger.status()

    @app.post("/api/log/stop")
    async def log_stop() -> dict[str, Any]:
        sup.logger.stop()
        sup._event("log", "logging stopped")
        return sup.logger.status()

    # -- ellipsometer sync ----------------------------------------------- #
    #  Post-run: put a *refit* FS-1 dynamic file back onto the reactor clock,
    #  using the (fs_time -> reactor_clock) sidecar captured live during the
    #  run. Read-only file handling; no hardware.

    def _in_data_dir(name: str) -> Path:
        d = sup.logger.dir
        p = (d / name).resolve()
        if d.resolve() not in p.parents or not p.exists():
            raise HTTPException(404, f"no such file in data dir: {name}")
        return p

    @app.get("/api/ellipsometer/sidecars")
    async def list_sidecars() -> dict[str, Any]:
        d = sup.logger.dir

        def entries(pattern: str) -> list[dict[str, Any]]:
            out: list[dict[str, Any]] = []
            if d.exists():
                for p in sorted(d.glob(pattern), reverse=True):
                    with contextlib.suppress(OSError):
                        st = p.stat()
                        out.append({"name": p.name, "size": st.st_size,
                                    "mtime": st.st_mtime})
            return out

        return {
            "dir": str(d),
            "sidecars": entries("*_ellipsometer.csv"),
            "reactor_runs": entries("*_run.csv"),
        }

    @app.post("/api/ellipsometer/merge")
    async def ellipsometer_merge(
        request: Request,
        sidecar: str,
        reactor_run: str = "",
        channels: str = "",
        filename: str = "refit",
    ) -> dict[str, Any]:
        """Body is the raw text of the refit .txt (no multipart dependency);
        `sidecar` and `reactor_run` are file names in the data dir. With a
        reactor_run the output is the combined plot-ready file (reactor channels
        + interpolated ellipsometry, keyed by cycle number, paused samples
        dropped); without it, ellipsometry alone on the reactor clock.
        `channels` optionally limits which reactor columns are included."""
        dyn_text = (await request.body()).decode("utf-8", errors="replace")
        if not dyn_text.strip():
            raise HTTPException(400, "empty refit file body")
        side_text = _in_data_dir(sidecar).read_text(encoding="utf-8", errors="replace")
        run_text = None
        if reactor_run:
            run_text = _in_data_dir(reactor_run).read_text(
                encoding="utf-8", errors="replace")
        chans = [c.strip() for c in channels.split(",") if c.strip()] or None
        try:
            result = ell.merge_text(dyn_text, side_text, run_text,
                                    reactor_channels=chans)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        tm = result.time_map
        stem = Path(filename).stem or "refit"
        return {
            "csv": result.to_csv(),
            "filename": f"{stem}_reactor_synced.csv",
            "mode": result.mode,
            "n_points": result.n_points,
            "time_map": {"a": tm.a, "b": tm.b, "n": tm.n,
                         "max_residual_s": tm.max_residual_s},
            "warnings": result.warnings,
            "reactor_channels": result.reactor_channels,
            "ellipsometry_columns": result.ellipsometry_columns,
        }

    return app
