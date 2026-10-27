"""Read-only bridge: retain live sensor samples until the updated server starts.

The current server has a one-hour memory buffer. This helper only calls its GET
endpoints and writes the same SQLite archive used by the updated server. It
exits after a replacement server advertises durable sensor history.
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path
from urllib.request import urlopen

from reactor.config import load_config
from reactor.sensor_archive import SensorArchive
from reactor.server.sensor_plot import sensor_plot_specs


def select_rows(rows, columns: set[str], last_t: float | None,
                interval: float = 5.0) -> list[dict]:
    chosen = []
    last = last_t if last_t is not None else float("-inf")
    for row in rows:
        t = row.get("t")
        if not isinstance(t, (int, float)) or t - last < interval:
            continue
        chosen.append({"t": t, **{column: row.get(column) for column in columns}})
        last = t
    return chosen


def fetch_json(base_url: str, route: str) -> dict:
    with urlopen(base_url + route, timeout=8) as response:
        return json.load(response)


def capture(archive: SensorArchive, columns: set[str], base_url: str,
            limit: int) -> int:
    rows = fetch_json(base_url, f"/api/trend?limit={limit}")["samples"]
    return archive.append_many(select_rows(rows, columns, archive.latest()))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--instance-id", required=True)
    args = parser.parse_args()
    cfg = load_config()
    archive = SensorArchive(Path(cfg.site.data_dir).expanduser().resolve()
                            / "sensor_history.sqlite3")
    archive.initialize()
    logging.basicConfig(filename=archive.path.with_suffix(".bridge.log"),
                        level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    columns = {spec.column for spec in sensor_plot_specs(cfg).values()}
    instance_id = args.instance_id
    unavailable_since = None
    first_poll = True
    logging.info("read-only bridge started for server %s", instance_id)
    while True:
        try:
            version = fetch_json(args.url, "/api/server/version")
            observed = version.get("instance_id")
            if observed != instance_id:
                plot = fetch_json(args.url, "/api/sensor-trend?key=pressure")
                if plot.get("archiveActive") is True:
                    logging.info("durable server %s is serving the archive; bridge exiting", observed)
                    return
                instance_id = observed
                logging.info("legacy server replaced; continuing bridge for %s", observed)
            inserted = capture(archive, columns, args.url,
                               18000 if first_poll or unavailable_since is not None else 300)
            if inserted:
                logging.info("saved %d sensor snapshots through %.3f",
                             inserted, archive.latest())
            unavailable_since = None
            first_poll = False
        except Exception as exc:
            logging.warning("read-only bridge poll failed: %s", exc)
            unavailable_since = unavailable_since or time.monotonic()
            if time.monotonic() - unavailable_since > 600:
                logging.error("server unavailable for ten minutes; bridge exiting")
                return
        time.sleep(3)


if __name__ == "__main__":
    main()
