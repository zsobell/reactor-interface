"""Enumerate the hardware actually present on this PC.

This replaces guessing channel names out of the old LabVIEW file. Run it on the
reactor computer and paste the results into config/reactor.yaml.

    python -m tools.discover_hardware
    python -m tools.discover_hardware --probe-modbus 192.168.0.0/24

STRICTLY READ-ONLY. It enumerates and identifies. It does not create tasks that
write, does not move a valve, does not change a setpoint, does not reset any
instrument. Safe to run with the reactor hot and under vacuum.
"""

from __future__ import annotations

import argparse
import ipaddress
import socket
import sys

# --------------------------------------------------------------------------- #
#  NI-DAQmx
# --------------------------------------------------------------------------- #


def discover_daqmx() -> None:
    print("=" * 74)
    print(" NI-DAQmx devices")
    print("=" * 74)
    try:
        import nidaqmx  # noqa: F401
        from nidaqmx.system import System
    except ImportError:
        print("  nidaqmx not installed.  ->  pip install nidaqmx")
        print("  (also needs the NI-DAQmx runtime, which LabVIEW already uses)")
        return
    except Exception as exc:  # driver present but unhappy
        print(f"  could not load nidaqmx: {exc}")
        return

    try:
        system = System.local()
        print(f"  driver version: {system.driver_version}")
    except Exception as exc:
        print(f"  could not reach the DAQmx driver: {exc}")
        return

    devices = list(system.devices)
    if not devices:
        print("  no devices found. Check NI MAX / that the chassis is powered.")
        return

    for dev in devices:
        try:
            print(f"\n  --- {dev.name} ---")
            print(f"      product type : {dev.product_type}")
            try:
                print(f"      serial       : {dev.serial_num:X}")
            except Exception:
                pass
            try:
                print(f"      simulated    : {dev.is_simulated}")
            except Exception:
                pass

            def show(title: str, items) -> None:
                names = [c.name for c in items]
                if names:
                    print(f"      {title} ({len(names)}):")
                    for n in names:
                        print(f"          {n}")

            show("analog  in ", dev.ai_physical_chans)
            show("analog  out", dev.ao_physical_chans)
            show("digital in ", dev.di_lines)
            show("digital out", dev.do_lines)

            # Which AI measurement types this module supports tells you whether
            # it is a thermocouple module, an RTD module, or plain voltage.
            try:
                types = [str(t).split(".")[-1] for t in dev.ai_meas_types]
                if types:
                    print(f"      AI measurement types: {', '.join(sorted(set(types)))}")
            except Exception:
                pass
        except Exception as exc:
            print(f"      error reading {dev.name}: {exc}")

    print("\n  Map these into config/reactor.yaml:")
    print("    pressure.channel      - needs a VOLTAGE input module")
    print("    stage_temp.channel    - needs a THERMOCOUPLE input module")
    print("    aux_inputs[].channel  - anything else you identify")
    print("    valves[].line         - a DIGITAL OUTPUT line")
    print("\n  Watch for module types that cannot do what you want: a current-output")
    print("  module has no ai channels, and a 9375's port0 is input-only.")


# --------------------------------------------------------------------------- #
#  VISA (ammeter and other bench instruments)
# --------------------------------------------------------------------------- #


def discover_visa() -> None:
    print()
    print("=" * 74)
    print(" VISA resources (ammeter, QCM controller, anything on GPIB/serial/LAN)")
    print("=" * 74)
    try:
        import pyvisa
    except ImportError:
        print("  pyvisa not installed.  ->  pip install pyvisa")
        return

    try:
        rm = pyvisa.ResourceManager()
    except Exception as exc:
        print(f"  no VISA backend: {exc}")
        print("  install NI-VISA, or use the pure-python backend: pip install pyvisa-py")
        return

    resources = rm.list_resources()
    if not resources:
        print("  none found.")
    for res in resources:
        print(f"\n  {res}")
        # *IDN? is the SCPI identity query. It is a read - it changes nothing.
        # But talking to an instrument puts it in REMOTE mode, where many meters
        # stop free-running and their display appears frozen. So hand control
        # back before closing.
        try:
            with rm.open_resource(res, open_timeout=1500) as inst:
                inst.timeout = 1500
                print(f"      *IDN? -> {inst.query('*IDN?').strip()}")
                from reactor.devices.instrument import return_to_local

                return_to_local(inst)
        except Exception as exc:
            print(f"      (no *IDN? response: {type(exc).__name__})")
    rm.close()


def discover_serial() -> None:
    print()
    print("=" * 74)
    print(" Serial ports")
    print("=" * 74)
    try:
        from serial.tools import list_ports
    except ImportError:
        print("  pyserial not installed.  ->  pip install pyserial")
        return
    ports = list(list_ports.comports())
    if not ports:
        print("  none found.")
    for p in ports:
        print(f"  {p.device:10s} {p.description}")
        if p.hwid:
            print(f"             {p.hwid}")


# --------------------------------------------------------------------------- #
#  Modbus TCP (MKS MFCs)
# --------------------------------------------------------------------------- #


def probe_modbus(cidr: str, port: int = 502, timeout: float = 0.25) -> None:
    """TCP-connect scan for Modbus listeners.

    Opens a socket and closes it. It does NOT send a Modbus request, so it
    cannot read or alter an MFC's state.
    """
    print()
    print("=" * 74)
    print(f" Scanning {cidr} for open TCP/{port} (Modbus)")
    print("=" * 74)
    try:
        net = ipaddress.ip_network(cidr, strict=False)
    except ValueError as exc:
        print(f"  bad network: {exc}")
        return

    hosts = list(net.hosts())
    if len(hosts) > 1024:
        print(f"  {len(hosts)} hosts is too many; use a /24 or smaller.")
        return

    found = []
    for ip in hosts:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            if s.connect_ex((str(ip), port)) == 0:
                found.append(str(ip))
                print(f"  OPEN  {ip}:{port}")
        finally:
            s.close()
    if not found:
        print("  nothing listening. Check the subnet and that the MFCs are powered.")
    else:
        print("\n  Put these in config/reactor.yaml under mfcs.devices[].host")
        print("  You still need each unit_id and full_scale_sccm (on the MFC label).")


def read_digital_inputs() -> None:
    """Read every digital INPUT line. Read-only - drives nothing.

    Worth doing before any output identification: if a control box wires valve
    position or status feedback back to the DAQ, these lines identify valves
    without energising anything.
    """
    print()
    print("=" * 74)
    print(" Digital inputs  (read-only - no output is driven)")
    print("=" * 74)
    try:
        import nidaqmx
        from nidaqmx.system import System
    except ImportError:
        print("  nidaqmx not installed.")
        return

    try:
        devices = list(System.local().devices)
    except Exception as exc:
        print(f"  could not reach the DAQmx driver: {exc}")
        return

    found_any = False
    for dev in devices:
        lines = [ln.name for ln in dev.di_lines]
        if not lines:
            continue
        found_any = True
        print(f"\n  --- {dev.name} ({dev.product_type}) : {len(lines)} input lines ---")
        try:
            with nidaqmx.Task() as task:
                for name in lines:
                    task.di_channels.add_di_chan(name)
                vals = task.read()
                if not isinstance(vals, list):
                    vals = [vals]
        except Exception as exc:
            print(f"      unreadable: {str(exc).splitlines()[0]}")
            continue
        high = [n for n, v in zip(lines, vals) if v]
        for name, v in zip(lines, vals):
            print(f"      {name:<28} {'HIGH' if v else 'low'}")
        print(f"      -> {len(high)} of {len(lines)} high")

    if not found_any:
        print("  no digital input lines on any device")
    else:
        print("\n  Toggle something on a control box by hand and re-run: a line that")
        print("  changes is feedback from whatever you moved.")


def dmm_status() -> None:
    """Report a DMM6500's current configuration and a few readings.

    STRICTLY READ-ONLY: every command here is a query. It does not select a
    function, change a range, autozero, or reset. It reports what the instrument
    is set to right now, which is what you need when a reading looks wrong.
    """
    print()
    print("=" * 74)
    print(" Keithley DMM6500 status  (read-only - nothing is configured)")
    print("=" * 74)
    try:
        import pyvisa
    except ImportError:
        print("  pyvisa not installed.")
        return

    from reactor.config import load_config

    try:
        cfg = load_config()
    except Exception as exc:
        print(f"  config error: {exc}")
        return

    targets = [i for i in cfg.instruments if i.resource]
    if not targets:
        print("  no instruments configured")
        return

    rm = pyvisa.ResourceManager()
    for icfg in targets:
        print(f"\n  --- {icfg.label or icfg.id}  ({icfg.resource}) ---")
        try:
            inst = rm.open_resource(icfg.resource, open_timeout=3000)
        except Exception as exc:
            print(f"      cannot open: {type(exc).__name__}: {exc}")
            continue
        try:
            inst.timeout = 8000
            inst.read_termination = "\n"
            inst.write_termination = "\n"

            def q(cmd: str) -> str:
                try:
                    return str(inst.query(cmd)).strip()
                except Exception as exc:
                    return f"<{type(exc).__name__}>"

            queries = [
                ("identity",        "*IDN?"),
                ("function",        ":SENS:FUNC?"),
                ("terminals",       ":ROUT:TERM?"),
                ("current range",   ":SENS:CURR:RANG?"),
                ("autorange",       ":SENS:CURR:RANG:AUTO?"),
                ("NPLC",            ":SENS:CURR:NPLC?"),
                ("autozero",        ":SENS:CURR:AZER?"),
                ("rel/offset on",   ":SENS:CURR:REL:STAT?"),
                ("rel/offset value", ":SENS:CURR:REL?"),
                ("averaging on",    ":SENS:CURR:AVER:STAT?"),
            ]
            width = max(len(n) for n, _ in queries)
            for name, cmd in queries:
                print(f"      {name:<{width}} : {q(cmd)}")

            print(f"\n      {'readings':<{width}} :", end=" ")
            vals = []
            for _ in range(6):
                r = q(":READ?")
                try:
                    vals.append(float(r.split(",")[0]))
                except ValueError:
                    pass
            print(", ".join(f"{v:.4e}" for v in vals) if vals else "none")
            if vals:
                mean = sum(vals) / len(vals)
                spread = max(vals) - min(vals)
                print(f"      {'mean':<{width}} : {mean:.4e} A")
                print(f"      {'spread':<{width}} : {spread:.4e} A"
                      f"   ({'NOISE - spread is comparable to the reading'
                            if spread > abs(mean) * 0.4 else 'stable'})")

            # Drain the queue rather than peeking at one entry: it is a FIFO, so
            # a single read leaves older complaints behind and you cannot tell a
            # stale one from a live one. Everything found is printed, so nothing
            # is hidden by clearing it.
            errs = []
            for _ in range(10):
                e = q(":SYST:ERR?")
                if not e or e.startswith(("0,", '0 ,')) or "No error" in e:
                    break
                errs.append(e)
            if errs:
                print(f"      {'error queue':<{width}} : {len(errs)} queued")
                for e in errs:
                    print(f"      {'':<{width}}   {e}")
            else:
                print(f"      {'error queue':<{width}} : clean")
        finally:
            # Hand the front panel back, via the USBTMC "go to local" interface
            # message. Not a SCPI command - see reactor/devices/instrument.py.
            from reactor.devices.instrument import return_to_local

            how = return_to_local(inst)
            print(f"      {'returned to local':<{width}} : "
                  f"{how or 'FAILED - press EXIT on the front panel'}")
            try:
                inst.close()
            except Exception:
                pass
    rm.close()

    print("\n  If the reading is orders of magnitude too small and the spread is")
    print("  comparable to the reading, the meter is measuring an open circuit.")
    print("  Usual causes, in order of how often they bite:")
    print("    * FRONT/REAR terminal switch does not match where the leads are")
    print("    * leads in the voltage terminals rather than the AMPS terminal")
    print("    * blown current fuse")
    print("    * the circuit under test is genuinely open or off")


def survey_inputs() -> None:
    """Read every analog input once and report which look connected.

    This is how to settle what the old log's A/B/C/D columns were without
    guessing: a floating input reads near-zero with large drift or sits at a
    rail; a connected sensor reads a stable, non-trivial value.

    Reads only. Creates no output tasks, commands nothing.
    """
    print()
    print("=" * 78)
    print(" Analog input survey  --  what is actually connected?")
    print("=" * 78)
    try:
        import nidaqmx
        from nidaqmx.system import System
    except ImportError:
        print("  nidaqmx not installed.  ->  pip install nidaqmx")
        return

    try:
        devices = list(System.local().devices)
    except Exception as exc:
        print(f"  could not reach the DAQmx driver: {exc}")
        return

    from nidaqmx.constants import TemperatureUnits, ThermocoupleType

    reserved: list[str] = []

    for dev in devices:
        chans = [c.name for c in dev.ai_physical_chans]
        if not chans:
            continue

        # Use a range the module actually supports - a thermocouple module is
        # +/-80 mV and rejects a +/-10 V request outright.
        try:
            rngs = list(dev.ai_voltage_rngs)
            vmax = max(rngs) if rngs else 10.0
        except Exception:
            vmax = 10.0
        # Decide TC vs voltage from the module's RANGE, not from ai_meas_types:
        # nidaqmx reports the same generic superset (including thermocouple and
        # strain gage) for every AI module, so it discriminates nothing. A
        # thermocouple module is millivolt-scale; a voltage module is volts.
        is_tc = vmax < 1.0

        print(f"\n  --- {dev.name} ({dev.product_type})"
              f"  range +/-{vmax:g} V{'  [thermocouple capable]' if is_tc else ''} ---")
        unit = "degC" if is_tc else "V"
        print(f"  {'channel':<24} {'mean ' + unit:>12} {'p-p':>10}  verdict")
        print(f"  {'-'*24} {'-'*12} {'-'*10}  {'-'*34}")

        for name in chans:
            samples = None
            err = ""
            try:
                with nidaqmx.Task() as task:
                    if is_tc:
                        # Reading as a thermocouple is more informative: an open
                        # TC reads absurdly high, a connected one reads ambient.
                        task.ai_channels.add_ai_thrmcpl_chan(
                            name, units=TemperatureUnits.DEG_C,
                            thermocouple_type=ThermocoupleType.K,
                        )
                    else:
                        task.ai_channels.add_ai_voltage_chan(
                            name, min_val=-vmax, max_val=vmax)
                    samples = [float(task.read(timeout=10.0)) for _ in range(6)]
            except Exception as exc:
                err = str(exc).splitlines()[0]

            if samples is None:
                if "reserved" in err.lower():
                    reserved.append(name)
                    note = "RESERVED by another program (see note below)"
                else:
                    note = f"unreadable: {err[:40]}"
                print(f"  {name:<24} {'-':>12} {'-':>10}  {note}")
                continue

            mean = sum(samples) / len(samples)
            pp = max(samples) - min(samples)

            if is_tc:
                if mean > 1000 or mean < -250:
                    verdict = "open circuit - no thermocouple attached"
                elif 5 < mean < 60 and pp < 3:
                    verdict = "STABLE near ambient - TC CONNECTED"
                elif pp > 20:
                    verdict = "very noisy - likely floating"
                else:
                    verdict = "STABLE SIGNAL - probably connected"
            else:
                if abs(mean) > vmax * 0.97:
                    verdict = "RAILED - open input or overrange"
                elif abs(mean) < 0.01 and pp < 0.02:
                    verdict = "quiet near zero - likely nothing"
                elif pp > max(0.5, abs(mean)):
                    verdict = "very noisy - likely floating"
                else:
                    verdict = "STABLE SIGNAL - probably connected"
            print(f"  {name:<24} {mean:>12.4f} {pp:>10.4f}  {verdict}")

    print("\n  Verdicts are heuristics, not proof. A 'stable signal' still needs")
    print("  you to identify what it is before it means anything.")

    if reserved:
        print()
        print("  " + "!" * 66)
        print("  RESERVED CHANNELS")
        print("  " + "!" * 66)
        print("  DAQmx gives one program exclusive use of a module's analog input.")
        print("  These channels could not be read because something else owns them")
        print("  right now - almost certainly the LabVIEW VI:")
        for name in reserved:
            print(f"      {name}")
        print()
        print("  Nothing was disturbed: DAQmx refused the request, it did not take")
        print("  the module over. But it does mean the two programs cannot read the")
        print("  same module at once - stop the LabVIEW VI before bring-up.")
        print("  It also tells you these channels are the ones actually in use.")


def read_pressure(expect: float | None) -> None:
    """Read the raw pressure voltage and work out which gauge curve fits.

    Reads one analog input. Commands nothing.
    """
    print()
    print("=" * 74)
    print(" Pressure gauge identification")
    print("=" * 74)

    from reactor.config import GAUGE_PRESETS, Scaling, load_config

    try:
        cfg = load_config()
    except Exception as exc:
        print(f"  config error: {exc}")
        return

    channel = cfg.pressure.channel
    print(f"  channel: {channel}")

    try:
        import nidaqmx
        from nidaqmx.constants import TerminalConfiguration
    except ImportError:
        print("  nidaqmx not installed.  ->  pip install nidaqmx")
        return

    term = {
        "rse": TerminalConfiguration.RSE,
        "nrse": TerminalConfiguration.NRSE,
        "diff": TerminalConfiguration.DIFF,
    }.get(cfg.pressure.terminal_config, TerminalConfiguration.RSE)

    try:
        with nidaqmx.Task() as task:
            task.ai_channels.add_ai_voltage_chan(
                channel,
                min_val=cfg.pressure.input_range_v[0],
                max_val=cfg.pressure.input_range_v[1],
                terminal_config=term,
            )
            samples = [float(task.read()) for _ in range(10)]
    except Exception as exc:
        print(f"  could not read {channel}: {type(exc).__name__}: {exc}")
        print("  Check the channel name against the DAQmx listing above.")
        return

    volts = sum(samples) / len(samples)
    spread = max(samples) - min(samples)
    print(f"  measured: {volts:.5f} V   (noise spread {spread:.5f} V over 10 reads)")
    print()
    print(f"  {'preset':<22} {'type':<8} implied pressure")
    print(f"  {'-'*22} {'-'*8} {'-'*22}")

    best, best_err = None, None
    for name in sorted(GAUGE_PRESETS):
        kind, gain, offset = GAUGE_PRESETS[name]
        p = Scaling(type=kind, gain=gain, offset=offset).apply(volts)
        mark = ""
        if expect is not None and p > 0:
            # Compare in decades - the right answer should be within ~0.1 decade.
            import math

            err = abs(math.log10(p) - math.log10(expect))
            if best_err is None or err < best_err:
                best, best_err = name, err
            if err < 0.1:
                mark = "  <== matches"
        print(f"  {name:<22} {kind:<8} {p:.4e} Torr{mark}")

    if expect is not None:
        print()
        print(f"  your gauge controller reads: {expect:.4e} Torr")
        if best_err is not None and best_err < 0.1:
            print(f"  BEST MATCH: {best}")
            print(f"  Put this in config/reactor.yaml:")
            print(f"      pressure:")
            print(f"        scaling:")
            print(f'          preset: "{best}"')
        else:
            off_by = f"{best_err:.2f} decades" if best_err is not None else "n/a"
            print(f"  No preset matches (closest: {best}, off by {off_by}).")
            _solve_gauge(volts, expect)


def _solve_gauge(volts: float, torr: float) -> None:
    """Work out the log curve from one (volts, pressure) pair.

    A log gauge obeys  log10(P) = gain*V + offset. One point fixes the offset
    once you assume a gain. Most controllers are 1 decade per volt, and their
    offsets are round numbers - so if the implied offset comes out near an
    integer, that identifies the family.
    """
    import math

    print()
    print("  " + "-" * 66)
    print("  SOLVING THE CURVE FROM YOUR READING")
    print("  " + "-" * 66)
    target = math.log10(torr)
    print(f"  measured {volts:.5f} V  ->  log10(P) must equal {target:.4f}")
    print()
    print(f"  {'gain (decades/V)':<20} {'implied offset':>16}   verdict")
    print(f"  {'-'*20} {'-'*16}   {'-'*28}")
    for gain, note in ((1.0, "1 decade/V - most common"),
                       (0.5, "2 V/decade"),
                       (2.0, "0.5 V/decade"),
                       (1.285347, "PKR-compatible")):
        offset = target - gain * volts
        near = round(offset)
        hit = abs(offset - near) < 0.06 and gain == 1.0
        mark = f"  <== round number ({near}), likely" if hit else f"  ({note})"
        print(f"  {gain:<20g} {offset:>16.4f}{mark}")

    print()
    print("  To use one of these, replace the preset in config/reactor.yaml with:")
    print("      scaling:")
    print("        type: \"log10\"")
    print("        gain: 1.0")
    print(f"        offset: {target - volts:.4f}")
    print()
    print("  ONE POINT IS NOT PROOF - it fits any gain you choose. Confirm it by")
    print("  taking a second reading at a very different pressure and checking the")
    print("  program agrees with the controller there too. If it does not, send me")
    print("  both (voltage, pressure) pairs and the curve solves exactly.")


def identify_mfc(host: str, port: int, unit_id: int) -> None:
    """Read-only identity/flow read from one MKS MFC over Modbus."""
    print()
    print("=" * 74)
    print(f" Identifying MFC at {host}:{port} unit {unit_id}")
    print("=" * 74)
    try:
        from reactor.devices.mks_mfc import probe_mfc
    except ImportError as exc:
        print(f"  {exc}  ->  pip install pymodbus")
        return
    for line in probe_mfc(host, port, unit_id):
        print(f"  {line}")


# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--probe-modbus", metavar="CIDR",
                    help="scan a subnet for Modbus TCP listeners, e.g. 192.168.0.0/24")
    ap.add_argument("--modbus-port", type=int, default=502)
    ap.add_argument("--identify-mfc", metavar="HOST",
                    help="read identity + flow from one MFC (read-only)")
    ap.add_argument("--unit-id", type=int, default=1)
    ap.add_argument("--skip-visa", action="store_true")
    ap.add_argument("--read-pressure", action="store_true",
                    help="read the pressure channel and identify the gauge curve")
    ap.add_argument("--expect", type=float, metavar="TORR",
                    help="the pressure your gauge controller currently displays, "
                         "e.g. --expect 5.9e-8")
    ap.add_argument("--survey-inputs", action="store_true",
                    help="read every analog input and report which look connected")
    ap.add_argument("--dmm-status", action="store_true",
                    help="report the DMM's configuration and readings (read-only)")
    ap.add_argument("--read-digital-inputs", action="store_true",
                    help="read every digital input line (read-only)")
    args = ap.parse_args(argv)

    print("Reactor hardware discovery - READ ONLY, nothing is commanded.\n")
    discover_daqmx()
    discover_serial()
    if not args.skip_visa:
        discover_visa()
    if args.read_digital_inputs:
        read_digital_inputs()
    if args.dmm_status:
        dmm_status()
    if args.survey_inputs:
        survey_inputs()
    if args.read_pressure or args.expect is not None:
        read_pressure(args.expect)
    if args.probe_modbus:
        probe_modbus(args.probe_modbus, args.modbus_port)
    if args.identify_mfc:
        identify_mfc(args.identify_mfc, args.modbus_port, args.unit_id)

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
