"""FS-1 ellipsometer stream decoder (reactor/devices/ellipsometer.py) against
REAL bytes captured off the instrument's port 4001 during a live dynamic run
(points 1, 2, 3 and the final point 62 of a 62-point, ~1 Hz acquisition).

This is the fake-hardware-boundary rule applied to a network instrument: the
bytes below are exactly what the FS-1 put on the wire, so if this passes the
decoder will read the real tool. Time values are cross-checked against the
independently-known cadence (~1 s spacing, first point at ~0.5 s).

Run directly: python -m tests.test_ellipsometer_decode
"""

from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, ".")

from reactor.devices.ellipsometer import (
    EllipsometerClient,
    EllipsometerPoint,
    SYNC,
    decode_record,
    iter_records,
)
from tests._support import Checker


def _hex(s: str) -> bytes:
    return bytes.fromhex(s.replace(" ", ""))


# Real records captured off 169.254.1.1:4001 (each is the 90-byte record that
# follows a 03 02 01 63 sync marker). Field order on the wire:
#   n, Fit_Diff, Thick(nm).1, AlignX, AlignY, Intensity, Time, Temp
REC = {
    1: _hex("08 01 6e 00 00 80 3f 08 46 69 74 5f 44 69 66 66 fa 84 8a 32 0b 54 68"
            "69 63 6b 28 6e 6d 29 2e 31 12 6e f4 33 06 41 6c 69 67 6e 58 f5 b9 6c"
            "3d 06 41 6c 69 67 6e 59 6d 14 71 be 09 49 6e 74 65 6e 73 69 74 79 5f"
            "fb be 3f 04 54 69 6d 65 a0 e9 ff 3e 04 54 65 6d 70 00 80 39 42"),
    2: _hex("08 01 6e 00 00 00 40 08 46 69 74 5f 44 69 66 66 c4 10 d5 38 0b 54 68"
            "69 63 6b 28 6e 6d 29 2e 31 cc 3b 2f 39 06 41 6c 69 67 6e 58 fa c5 70"
            "3d 06 41 6c 69 67 6e 59 01 71 70 be 09 49 6e 74 65 6e 73 69 74 79 c8"
            "f0 be 3f 04 54 69 6d 65 bf c2 bd 3f 04 54 65 6d 70 00 80 39 42"),
    3: _hex("08 01 6e 00 00 40 40 08 46 69 74 5f 44 69 66 66 35 b0 d2 38 0b 54 68"
            "69 63 6b 28 6e 6d 29 2e 31 ed 2e c3 3a 06 41 6c 69 67 6e 58 e5 c4 77"
            "3d 06 41 6c 69 67 6e 59 14 37 71 be 09 49 6e 74 65 6e 73 69 74 79 98"
            "ed be 3f 04 54 69 6d 65 0b d0 1d 40 04 54 65 6d 70 00 a0 39 42"),
    62: _hex("08 01 6e 00 00 78 42 08 46 69 74 5f 44 69 66 66 6f 26 f2 38 0b 54 68"
             "69 63 6b 28 6e 6d 29 2e 31 42 14 12 3b 06 41 6c 69 67 6e 58 1c 41 72"
             "3d 06 41 6c 69 67 6e 59 c6 29 70 be 09 49 6e 74 65 6e 73 69 74 79 b9"
             "f3 be 3f 04 54 69 6d 65 e5 8f 71 42 04 54 65 6d 70 00 80 39 42"),
}

# Independently-known expected relative-time of each point (seconds), from the
# ~1 Hz cadence (first sample lands at ~half the 1 s integration window).
EXPECT_TIME = {1: 0.4998, 2: 1.4825, 3: 2.4658, 62: 60.392}


def _wire(*indices: int) -> bytes:
    """SYNC + record for each requested point, concatenated as on the wire."""
    return b"".join(SYNC + REC[i] for i in indices)


async def main() -> int:
    c = Checker("test_ellipsometer_decode")

    # -- pure record decode ------------------------------------------------- #
    c.section("decode_record: one real 90-byte record")
    fields, nxt = decode_record(REC[1])
    c.check("consumed exactly 90 bytes", nxt == len(REC[1]), f"nxt={nxt}")
    c.check("has all 8 fields", len(fields) == 8, f"{list(fields)}")
    for name in ("n", "Fit_Diff", "Thick(nm).1", "AlignX", "AlignY",
                 "Intensity", "Time", "Temp"):
        c.check(f"field {name!r} present", name in fields)
    c.check("n == 1", fields.get("n") == 1.0, f"n={fields.get('n')}")
    c.check("Time ~ 0.4998", abs(fields["Time"] - EXPECT_TIME[1]) < 0.01,
            f"Time={fields['Time']:.4f}")
    c.check("Temp ~ 46.4", abs(fields["Temp"] - 46.375) < 0.05,
            f"Temp={fields['Temp']:.3f}")
    c.check("Thick ~ 0 (bare substrate)", abs(fields["Thick(nm).1"]) < 1e-3,
            f"Thick={fields['Thick(nm).1']:.3e}")

    # -- walk a multi-record stream ---------------------------------------- #
    c.section("iter_records: full 4-point stream")
    recs = list(iter_records(_wire(1, 2, 3, 62)))
    c.check("decoded 4 records", len(recs) == 4, f"got {len(recs)}")
    idx = [int(round(r["n"])) for r in recs]
    c.check("indices are 1,2,3,62", idx == [1, 2, 3, 62], f"{idx}")
    times_ok = all(abs(r["Time"] - EXPECT_TIME[i]) < 0.01
                   for i, r in zip([1, 2, 3, 62], recs))
    c.check("all Time fields match cadence", times_ok,
            f"{[round(r['Time'], 3) for r in recs]}")
    # Time must increase monotonically - it is the join key to the refit file
    c.check("Time strictly increasing",
            all(recs[k]["Time"] < recs[k + 1]["Time"] for k in range(3)))

    # -- resync: garbage before the first marker --------------------------- #
    c.section("iter_records: resync past leading garbage")
    noisy = _hex("de ad be ef 00 03 02") + _wire(1, 2)
    recs2 = list(iter_records(noisy))
    c.check("still decodes both points", [int(round(r["n"])) for r in recs2] == [1, 2],
            f"{[int(round(r['n'])) for r in recs2]}")

    # -- EllipsometerPoint mapping ----------------------------------------- #
    c.section("EllipsometerPoint.from_fields")
    pt = EllipsometerPoint.from_fields(fields, t_recv=123.5)
    c.check("index == 1", pt.index == 1)
    c.check("thickness_unit parsed as 'nm'", pt.thickness_unit == "nm",
            f"unit={pt.thickness_unit!r}")
    c.check("time_s carried through", pt.time_s is not None
            and abs(pt.time_s - EXPECT_TIME[1]) < 0.01)
    c.check("t_recv preserved", pt.t_recv == 123.5)

    # -- streaming client: SYNC and record split across TCP segments ------- #
    c.section("EllipsometerClient._read_loop over a StreamReader")
    reader = asyncio.StreamReader()
    # Feed the way TCP really delivered it: the 4-byte SYNC arrives separately
    # from the 90-byte record (that split is exactly what the capture showed).
    for i in (1, 2, 3, 62):
        reader.feed_data(SYNC)
        reader.feed_data(REC[i])
    reader.feed_eof()

    got: list[EllipsometerPoint] = []
    client = EllipsometerClient("test-host", on_point=got.append)
    try:
        await client._read_loop(reader)
    except asyncio.IncompleteReadError:
        pass                       # EOF after the last record - expected
    c.check("client decoded 4 points", len(got) == 4, f"got {len(got)}")
    c.check("client indices 1,2,3,62", [p.index for p in got] == [1, 2, 3, 62],
            f"{[p.index for p in got]}")
    c.check("client stamped a reactor clock", all(p.t_recv > 0 for p in got))
    c.check("points_seen counter == 4", client.points_seen == 4,
            f"{client.points_seen}")

    return c.summary()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
