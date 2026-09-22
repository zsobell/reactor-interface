"""Aperture replacement API is confirmed, idempotent, and non-actuating."""
from __future__ import annotations

import asyncio

from reactor.server.app import create_app
from reactor.testing.virtual_reactor import VirtualReactor
from tests._support import Checker, asgi_call


async def main() -> int:
    c = Checker("test_aperture_api")
    async with VirtualReactor() as vr:
        app = create_app(supervisor=vr.sup)
        await vr.tick()
        state_status, state = await asgi_call(app, "GET", "/api/state")
        aperture = state["aperture_lifetime"]
        current_id = aperture["current"]["id"]
        c.check("normal state API carries the complete aperture record",
                state_status == 200 and aperture["available"]
                and aperture["active"] is False
                and isinstance(aperture["history"], list))

        before_writes = list(vr.daq.do_writes)
        status, body = await asgi_call(app, "POST", "/api/aperture/replaced", {
            "expected_aperture_id": current_id, "confirm": False,
        })
        c.check("server refuses an unconfirmed first click",
                status == 400 and "confirm" in body["detail"].lower())
        status, body = await asgi_call(app, "POST", "/api/aperture/replaced", {
            "expected_aperture_id": "stale-id", "confirm": True,
        })
        c.check("unknown stale identity is a conflict", status == 409)

        status, body = await asgi_call(app, "POST", "/api/aperture/replaced", {
            "expected_aperture_id": current_id, "confirm": True,
        })
        replaced = body["aperture_lifetime"]
        c.check("confirmed replacement archives and starts a new identity",
                status == 200 and len(replaced["history"]) == 1
                and replaced["history"][0]["id"] == current_id
                and replaced["current"]["id"] != current_id)
        event_count = len(vr.sup.events)
        status, replay = await asgi_call(app, "POST", "/api/aperture/replaced", {
            "expected_aperture_id": current_id, "confirm": True,
        })
        c.check("a retried response cannot archive or log twice",
                status == 200
                and len(replay["aperture_lifetime"]["history"]) == 1
                and len(vr.sup.events) == event_count)
        c.check("all replacement API paths are hardware read-only",
                vr.daq.do_writes == before_writes)

        vr.supplies["hv"].hv_on = True
        await vr.tick()
        active_id = vr.sup.aperture_lifetime.snapshot()["current"]["id"]
        status, active = await asgi_call(app, "POST", "/api/aperture/replaced", {
            "expected_aperture_id": active_id, "confirm": True,
        })
        c.check("active beam timing blocks an impossible replacement",
                status == 409 and "active" in active["detail"])

    return c.summary()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
