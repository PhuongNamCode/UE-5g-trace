#!/usr/bin/env python3
"""Read-only 5G drop trace for the Pegatron FM160 on this PC.

Polls the Ethernet link and the Fibocom web API, and runs SCAT on the DIAG
serial port only. It never passes --enforce and never radio-cycles the modem.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import signal
import stat
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DETECT = Path("/home/fcp/5Gmodem/detect.sh")
CACHE = Path("/home/fcp/5gmon/cache.json")
COMMON = Path("/home/fcp/5gmon/dongles/common.py")
CACHE_MAX_AGE = "120"
READER = Path("/home/fcp/5gmon/dongles/pegatron_fm160.py")
SCAT_SRC = Path(
    "/home/fcp/Namnp-SUTD/ID_IM_rapp/neuroran-cell-monitor-app/"
    "neuroran-cell-monitor-qualcomm-app/scat/src"
)
DIAG_PORT = Path("/dev/ttyUSB0")
FORBIDDEN_PORTS = (Path("/dev/ttyUSB1"), Path("/dev/ttyUSB2"))
NET_TARGET = "8.8.8.8"
RSRP_COLLAPSE = -110
SINR_COLLAPSE = 0

# SCAT prints these phrases for the three log codes the coverage table uses.
CODE_MARKERS = (
    ("0xB821", "NR RRC OTA Packet"),
    ("0xB80C", "5GMM State:"),
    ("0xB97F", "NR ML1 Meas Packet"),
)

PAUSE_REMINDER = (
    "Pause the 5gmon cron before a drop trace. check.sh runs airplane mode "
    "when this Pegatron goes DOWN and hides the registration state.\n"
    "  crontab -l | grep -v '/home/fcp/5gmon/check.sh' | crontab -\n"
    "Restore it when the trace ends:\n"
    "  (crontab -l 2>/dev/null; echo '*/5 * * * * /home/fcp/5gmon/check.sh') | crontab -\n"
    "This helper does not change that crontab."
)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def detect_dongle() -> tuple[str, str, str]:
    result = subprocess.run(
        ["bash", str(DETECT)],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    parts = (result.stdout or "").split()
    parts += [""] * (3 - len(parts))
    return parts[0], parts[1], parts[2]


def port_owners(device: Path) -> str:
    fuser = subprocess.run(
        ["fuser", str(device)],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if fuser.returncode == 0:
        return (fuser.stdout + fuser.stderr).strip()
    return ""


def assert_diag_free(device: Path) -> None:
    if not device.exists() or not stat.S_ISCHR(device.stat().st_mode):
        raise RuntimeError(f"DIAG port is not a character device: {device}")
    owners = port_owners(device)
    if owners:
        raise RuntimeError(f"{device} is already open: {owners}")


def parse_signal_line(line: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for token in line.split():
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        fields[key] = value
    return fields


def number(value: str | None) -> float | None:
    if value is None or value in {"", "na", "-"}:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def ping(iface: str, target: str) -> tuple[float | None, float | None]:
    result = subprocess.run(
        ["ping", "-c", "1", "-W", "2", "-I", iface, target],
        check=False,
        capture_output=True,
        text=True,
        timeout=12,
    )
    text = result.stdout or ""
    loss = None
    rtt = None
    loss_match = re.search(r"(\d+(?:\.\d+)?)%\s+packet loss", text)
    if loss_match:
        loss = float(loss_match.group(1))
    rtt_match = re.search(r"rtt .* = [\d.]+/([\d.]+)/", text)
    if rtt_match:
        rtt = float(rtt_match.group(1))
    if loss is None:
        loss = 100.0
    return loss, rtt


def ipv4_of(iface: str) -> str:
    result = subprocess.run(
        ["ip", "-4", "-o", "addr", "show", iface],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    match = re.search(r"inet (\S+)", result.stdout or "")
    return match.group(1) if match else ""


def carrier_of(iface: str) -> str:
    path = Path(f"/sys/class/net/{iface}/carrier")
    try:
        return path.read_text(encoding="utf-8").strip() or "0"
    except OSError:
        return "missing"


def blank_signal(fields: dict[str, str]) -> bool:
    return fields.get("rsrp", "na") in {"", "na"} and fields.get("reg", "na") in {"", "na"}


def read_cache() -> dict[str, str]:
    """Same fresh-cache read signal.sh uses, so a poll does not open a second web session."""
    if not COMMON.is_file() or not CACHE.is_file():
        return {}
    try:
        result = subprocess.run(
            [sys.executable, str(COMMON), "cache-line", str(CACHE), CACHE_MAX_AGE, "pegatron_fm160"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        return {}
    line = (result.stdout or "").strip().splitlines()
    fields = parse_signal_line(line[-1] if line else "")
    if blank_signal(fields):
        return {}
    return fields


def read_signal() -> tuple[str, dict[str, str]]:
    """One read-only signal line. Never passes --enforce."""
    cached = read_cache()
    if cached:
        return "cache", cached
    command = [sys.executable, str(READER), "--json", "--no-config"]
    if "--enforce" in command:
        raise RuntimeError("refusing to pass --enforce")
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=25,
        )
    except subprocess.TimeoutExpired:
        return "busy", {}
    text = (result.stdout or "").strip().splitlines()
    raw = text[-1] if text else ""
    if not raw.startswith("{"):
        fields = parse_signal_line(raw)
        if blank_signal(fields):
            return "busy", fields
        return "ok", fields
    try:
        import json

        report = json.loads(raw)
    except json.JSONDecodeError:
        return "busy", {}
    fields = parse_signal_line(report.get("signal_line") or "")
    if report.get("busy") or report.get("errors") or blank_signal(fields):
        return "busy", fields
    radio = report.get("radio") or {}
    for key in ("rsrp", "rsrq", "sinr", "band", "arfcn", "pci", "reg", "data", "rat"):
        if key in radio and radio[key] is not None and key not in fields:
            fields[key] = str(radio[key])
    return "ok", fields


def is_drop(row: dict[str, str]) -> bool:
    return classify_row(row) is not None


def classify_row(row: dict[str, str]) -> str | None:
    """Return usb, registration, radio, or data when this sample is a loss."""
    carrier = row.get("carrier") or ""
    gw_loss = number(row.get("gw_loss"))
    inet_loss = number(row.get("inet_loss"))
    reg = row.get("reg") or "na"
    data = row.get("data") or "na"
    rsrp = number(row.get("rsrp"))
    sinr = number(row.get("sinr"))
    web = row.get("web") or ""
    gw_up = gw_loss is not None and gw_loss < 100
    if carrier != "1" or not gw_up:
        return "usb"
    if web != "busy" and reg not in {"", "na", "registered"}:
        return "registration"
    radio_collapsed = (rsrp is not None and rsrp < RSRP_COLLAPSE) or (
        sinr is not None and sinr < SINR_COLLAPSE
    )
    if reg == "registered" and radio_collapsed:
        return "radio"
    inet_down = inet_loss is not None and inet_loss >= 100
    data_down = data == "down"
    rsrp_healthy = rsrp is None or rsrp >= RSRP_COLLAPSE
    if reg == "registered" and rsrp_healthy and (inet_down or data_down):
        return "data"
    return None


def sample(iface: str, gateway: str) -> dict[str, str]:
    web, fields = read_signal()
    gw_loss, gw_rtt = ping(iface, gateway) if gateway else (100.0, None)
    inet_loss, inet_rtt = ping(iface, NET_TARGET)
    row = {
        "timestamp": utc_now(),
        "carrier": carrier_of(iface),
        "ipv4": ipv4_of(iface),
        "gw": gateway,
        "gw_loss": "" if gw_loss is None else f"{gw_loss:.0f}",
        "gw_rtt_ms": "" if gw_rtt is None else f"{gw_rtt:.1f}",
        "inet_loss": "" if inet_loss is None else f"{inet_loss:.0f}",
        "inet_rtt_ms": "" if inet_rtt is None else f"{inet_rtt:.1f}",
        "web": web,
        "rsrp": fields.get("rsrp", "na"),
        "rsrq": fields.get("rsrq", "na"),
        "sinr": fields.get("sinr", "na"),
        "band": fields.get("band", "na"),
        "arfcn": fields.get("arfcn", "na"),
        "pci": fields.get("pci", "na"),
        "reg": fields.get("reg", "na"),
        "data": fields.get("data", "na"),
        "rat": fields.get("rat", "na"),
    }
    return row


CSV_FIELDS = [
    "timestamp",
    "carrier",
    "ipv4",
    "gw",
    "gw_loss",
    "gw_rtt_ms",
    "inet_loss",
    "inet_rtt_ms",
    "web",
    "rsrp",
    "rsrq",
    "sinr",
    "band",
    "arfcn",
    "pci",
    "reg",
    "data",
    "rat",
]


def start_scat(pcap: Path, log_path: Path, qmdl: Path) -> subprocess.Popen[str]:
    command = [
        sys.executable,
        str(ROOT / "scat_entry.py"),
        "-t",
        "qc",
        "-s",
        str(DIAG_PORT),
        "-L",
        "rrc,nas,mac",
        "-F",
        str(pcap),
        "--qmdl",
        str(qmdl),
    ]
    if any(port.name in command for port in FORBIDDEN_PORTS):
        raise RuntimeError("refusing to open an AT port")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SCAT_SRC) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    log = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
        start_new_session=True,
    )

    def stamp() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            log.write(f"{utc_now()} {line}")
            log.flush()
        log.close()

    thread = threading.Thread(target=stamp, name="scat-log", daemon=True)
    thread.start()
    process.scat_log_thread = thread  # type: ignore[attr-defined]
    return process


def stop_scat(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGINT)
        except ProcessLookupError:
            pass
        else:
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                process.wait(timeout=5)
    thread = getattr(process, "scat_log_thread", None)
    if thread is not None:
        thread.join(timeout=2)


def count_codes(log_path: Path) -> dict[str, int]:
    text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
    return {code: text.count(marker) for code, marker in CODE_MARKERS}


def write_coverage(path: Path, timing: Path) -> dict:
    """Count every table log code in the raw DIAG timing file."""
    from scat_entry import ROWS

    seen: dict[str, int] = {}
    if timing.is_file():
        for line in timing.read_text(encoding="utf-8", errors="replace").splitlines()[1:]:
            parts = line.split(",")
            if len(parts) < 2:
                continue
            code = parts[1].upper()
            seen[code] = seen.get(code, 0) + 1
    rows = [
        {"code": code, "name": name, "decoder": decoder, "count": seen.get(code, 0)}
        for code, name, decoder in ROWS
    ]
    payload = {"rows": rows}
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def write_notes(
    path: Path,
    *,
    dongle: str,
    iface: str,
    gateway: str,
    rows: list[dict[str, str]],
    kind: str,
    counts: dict[str, int],
    scat_rc: int | None,
) -> None:
    usable = all(counts.get(code, 0) > 0 for code, _marker in CODE_MARKERS)
    lines = [
        f"dongle={dongle} iface={iface} gw={gateway}",
        f"samples={len(rows)} class={kind}",
        "codes " + " ".join(f"{code}={counts.get(code, 0)}" for code, _ in CODE_MARKERS),
        f"usable={'yes' if usable else 'no'} scat_exit={scat_rc if scat_rc is not None else 'running'}",
        "",
        "A usable Pegatron capture contains NR RRC 0xB821, 5GMM state 0xB80C, and PHY 0xB97F.",
        "An idle registered UE can emit 0xB97F the whole time and still omit 0xB821 and 0xB80C",
        "until there is RRC signaling or a 5GMM state change. That is the preliminary Pegatron column.",
        "The APAL Compal has no DIAG port, so those codes do not apply to it.",
        "The 2026-11-24 22:20 APAL outage was a registration loss: reg=searching, internet loss 100%,",
        "gateway ping still 0% loss, carrier stayed 1. It is not a sample of this Pegatron.",
        "",
        PAUSE_REMINDER,
        "",
    ]
    if rows:
        last = rows[-1]
        lines.append(
            "last "
            + " ".join(
                f"{key}={last.get(key, '')}"
                for key in ("carrier", "reg", "data", "rsrp", "sinr", "inet_loss", "gw_loss", "web")
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def trace(duration: float | None, interval: float, tail: float) -> int:
    dongle, iface, gateway = detect_dongle()
    if dongle != "pegatron_fm160":
        print(
            f"refusing to trace dongle={dongle or 'none'} iface={iface or 'none'}. "
            "This helper runs only for pegatron_fm160. The APAL has no DIAG port.",
            file=sys.stderr,
        )
        return 2
    assert_diag_free(DIAG_PORT)
    print(PAUSE_REMINDER, file=sys.stderr)
    out = ROOT / "captures" / utc_stamp()
    out.mkdir(parents=True, exist_ok=True)
    csv_path = out / "timeline.csv"
    log_path = out / "scat.log"
    pcap = out / "drop.pcap"
    qmdl = out / "diag.qmdl"
    notes = out / "notes.txt"
    print(f"writing {out}", flush=True)
    process = start_scat(pcap, log_path, qmdl)
    rows: list[dict[str, str]] = []
    kind: str | None = None
    drop_at: float | None = None
    started = time.monotonic()
    try:
        with csv_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
            writer.writeheader()
            while True:
                row = sample(iface, gateway)
                rows.append(row)
                writer.writerow(row)
                handle.flush()
                print(
                    f"{row['timestamp']} reg={row['reg']} data={row['data']} "
                    f"rsrp={row['rsrp']} inet_loss={row['inet_loss']}% web={row['web']}",
                    flush=True,
                )
                found = classify_row(row)
                if found and drop_at is None:
                    kind = found
                    drop_at = time.monotonic()
                    print(f"loss class={kind}; logging {tail:.0f}s more", flush=True)
                if drop_at is not None and time.monotonic() - drop_at >= tail:
                    break
                if duration is not None and time.monotonic() - started >= duration:
                    break
                remaining = interval
                if drop_at is not None:
                    remaining = min(interval, max(0.0, tail - (time.monotonic() - drop_at)))
                if duration is not None:
                    remaining = min(remaining, max(0.0, duration - (time.monotonic() - started)))
                if remaining > 0:
                    time.sleep(remaining)
    finally:
        stop_scat(process)
    if kind is None:
        kind = "up"
    counts = count_codes(log_path)
    coverage = write_coverage(out / "coverage.json", out / "diag.timing.csv")
    write_notes(
        notes,
        dongle=dongle,
        iface=iface,
        gateway=gateway,
        rows=rows,
        kind=kind,
        counts=counts,
        scat_rc=process.poll(),
    )
    usable = all(counts.get(code, 0) > 0 for code, _marker in CODE_MARKERS)
    print(
        f"class={kind} usable={'yes' if usable else 'no'} "
        + " ".join(f"{code}={counts.get(code, 0)}" for code, _ in CODE_MARKERS),
        flush=True,
    )
    print("raw " + " ".join(f"{row['code']}={row['count']}" for row in coverage["rows"]), flush=True)
    print(f"notes {notes}", flush=True)
    return 0 if kind == "up" or usable or kind in {"usb", "registration", "radio", "data"} else 1


def self_test() -> int:
    """The 22:20 APAL shape is a registration loss with the USB link still up."""
    apal = {
        "carrier": "1",
        "gw_loss": "0",
        "inet_loss": "100",
        "reg": "searching",
        "data": "up",
        "rsrp": "na",
        "sinr": "na",
        "web": "ok",
    }
    usb = dict(apal, carrier="0", gw_loss="100", reg="registered")
    radio = {
        "carrier": "1",
        "gw_loss": "0",
        "inet_loss": "0",
        "reg": "registered",
        "data": "up",
        "rsrp": "-120",
        "sinr": "5",
        "web": "ok",
    }
    data = {
        "carrier": "1",
        "gw_loss": "0",
        "inet_loss": "100",
        "reg": "registered",
        "data": "down",
        "rsrp": "-66",
        "sinr": "30",
        "web": "ok",
    }
    healthy = dict(data, inet_loss="0", data="up")
    expected = {
        "apal-22:20": "registration",
        "usb": "usb",
        "radio": "radio",
        "data": "data",
        "up": None,
    }
    got = {
        "apal-22:20": classify_row(apal),
        "usb": classify_row(usb),
        "radio": classify_row(radio),
        "data": classify_row(data),
        "up": classify_row(healthy),
    }
    if got != expected:
        print(f"self-test failed: {got}", file=sys.stderr)
        return 1
    print("self-test ok: 22:20 shape is registration with USB still up")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Trace a Pegatron FM160 5G drop from DIAG and the web API.")
    parser.add_argument("--duration", type=float, default=None, help="Stop after this many seconds even if the link stays up")
    parser.add_argument("--interval", type=float, default=10.0)
    parser.add_argument("--tail", type=float, default=60.0, help="Seconds to keep logging after a loss")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    return trace(args.duration, args.interval, args.tail)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("stopped", file=sys.stderr)
        sys.exit(130)
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        sys.exit(2)
