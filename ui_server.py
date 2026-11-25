#!/usr/bin/env python3
"""Local viewer for Pegatron UE status and SCAT traces.

Reads the 5gmon cache and files under captures/. It does not open DIAG,
does not log into the dongle web UI, and does not radio-cycle the modem.
"""

from __future__ import annotations

import csv
import json
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
UI = ROOT / "ui"
CAPTURES = ROOT / "captures"
CACHE = Path("/home/fcp/5gmon/cache.json")
HOST = "127.0.0.1"
PORT = 8765
STAMP = re.compile(r"^[0-9]{8}T[0-9]{6}Z$")
CAPTURE_JOB = {"running": False, "message": "idle"}
QMDL_CACHE: dict[tuple[str, int], list[dict]] = {}
LINE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\s+(.*)$")
RADIO = re.compile(r"^Radio \d+:\s*")

KINDS = {
    "NR ML1 Meas Packet": "phy",
    "NR RRC OTA Packet": "rrc",
    "NR RRC SCell Info": "rrc",
    "NR MIB": "rrc",
    "5GMM State:": "nas",
    "NAS-5GS": "nas",
}


def load_cache() -> dict:
    try:
        data = json.loads(CACHE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"ok": False, "error": "5gmon cache is unreadable"}
    report = data.get("report") or {}
    radio = report.get("radio") or {}
    link = report.get("link") or {}
    updated = data.get("updated") or 0
    age = round(time.time() - updated, 1) if updated else None
    keep_radio = (
        "rat", "rsrp", "rsrq", "sinr", "band", "arfcn", "pci", "cell",
        "tac", "bw", "reg", "data", "plmn",
    )
    return {
        "ok": True,
        "age_s": age,
        "stale": age is None or age > 120 or bool(report.get("radio_stale")),
        "status": report.get("status"),
        "dongle": report.get("dongle"),
        "iface": report.get("iface"),
        "gateway": report.get("gateway"),
        "time": report.get("time"),
        "errors": report.get("errors") or [],
        "radio_age_s": report.get("radio_age_s"),
        "signal_line": report.get("signal_line") or "",
        "radio": {key: radio.get(key) for key in keep_radio},
        "link": {
            "carrier": link.get("carrier"),
            "gw_loss": link.get("gw_loss"),
            "gw_rtt": link.get("gw_rtt"),
            "inet_loss": link.get("inet_loss"),
            "inet_rtt": link.get("inet_rtt"),
            "oper": link.get("oper"),
        },
    }


def list_captures() -> list[dict]:
    if not CAPTURES.is_dir():
        return []
    found = []
    for path in sorted(CAPTURES.iterdir(), reverse=True):
        if path.is_dir() and STAMP.match(path.name):
            notes = parse_notes(path / "notes.txt")
            found.append({"id": path.name, **notes})
    return found


def parse_notes(path: Path) -> dict:
    text = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
    lines = text.splitlines()
    head = " ".join(lines[:4])
    fields = dict(re.findall(r"([A-Za-z0-9_]+)=([^\s]+)", head))
    code_line = next((line for line in lines if line.startswith("codes ")), "")
    codes = {key: int(value) for key, value in re.findall(r"(0x[0-9A-Fa-f]+)=(\d+)", code_line)}
    return {
        "dongle": fields.get("dongle", ""),
        "iface": fields.get("iface", ""),
        "gw": fields.get("gw", ""),
        "samples": fields.get("samples", ""),
        "class": fields.get("class", ""),
        "usable": fields.get("usable", "no"),
        "codes": codes,
    }


def qmdl_events(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    key = (str(path), path.stat().st_mtime_ns)
    cached = QMDL_CACHE.get(key)
    if cached is None:
        from scat_entry import read_qmdl

        QMDL_CACHE.clear()
        cached = read_qmdl(path)
        QMDL_CACHE[key] = cached
    return cached


def start_capture() -> bool:
    if CAPTURE_JOB["running"]:
        return False

    def run() -> None:
        CAPTURE_JOB["running"] = True
        CAPTURE_JOB["message"] = "reading DIAG from the dongle"
        try:
            result = subprocess.run(
                [sys.executable, str(ROOT / "trace_drop.py"), "--duration", "20"],
                cwd=ROOT,
                check=False,
                timeout=120,
            )
            CAPTURE_JOB["message"] = f"finished exit {result.returncode}"
        except subprocess.TimeoutExpired:
            CAPTURE_JOB["message"] = "timed out"
        except OSError as error:
            CAPTURE_JOB["message"] = str(error)
        finally:
            CAPTURE_JOB["running"] = False

    threading.Thread(target=run, name="dongle-read", daemon=True).start()
    return True


def read_json(path: Path):
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def read_timeline(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def payload_of(body: str) -> str:
    return RADIO.sub("", body, count=1)


def kind_of(payload: str) -> str | None:
    for prefix, kind in KINDS.items():
        if payload.startswith(prefix):
            return kind
    return None


def is_continuation(payload: str, current_kind: str | None) -> bool:
    if current_kind is None:
        return False
    if payload.startswith(("Layer ", "└", "    ")):
        return True
    if current_kind == "rrc" and payload.startswith("NR RRC OTA Packet:"):
        return True
    return False


def summarize(kind: str, lines: list[str]) -> str:
    text = "\n".join(lines)
    if kind == "phy":
        cell = re.search(
            r"Cell \d+: PCI:\s*(\d+).*RSRP:\s*(-?\d+(?:\.\d+)?),\s*RSRQ:\s*(-?\d+(?:\.\d+)?)",
            text,
        )
        arfcn = re.search(r"NR-ARFCN:\s*(\d+)", text)
        if cell and arfcn:
            return f"PCI {cell.group(1)}  ARFCN {arfcn.group(1)}  RSRP {cell.group(2)}  RSRQ {cell.group(3)}"
        return lines[0][:160]
    if kind == "rrc":
        typed = re.search(r"Type:\s*([^,\n]+)", text)
        arfcn = re.search(r"NR-ARFCN:\s*(\d+)", text)
        pci = re.search(r"PCI:\s*(\d+)", text)
        parts = ["NR RRC"]
        if typed:
            parts.append(typed.group(1).strip())
        if pci:
            parts.append(f"PCI {pci.group(1)}")
        if arfcn:
            parts.append(f"ARFCN {arfcn.group(1)}")
        return "  ".join(parts)
    if kind == "nas":
        return lines[0][:180]
    return lines[0][:180]


def parse_scat(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    events: list[dict] = []
    current: dict | None = None

    def flush() -> None:
        nonlocal current
        if current is None:
            return
        current["summary"] = summarize(current["kind"], current["lines"])
        current["text"] = "\n".join(current["lines"])
        events.append(current)
        current = None

    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = LINE.match(raw)
        if not match:
            if current is not None:
                current["lines"].append(raw)
            continue
        timestamp, body = match.group(1), match.group(2)
        payload = payload_of(body)
        if current is not None and is_continuation(payload, current["kind"]):
            current["lines"].append(payload)
            continue
        flush()
        kind = kind_of(payload)
        if kind is None:
            if "INFO:" in payload or payload.startswith(("Radio 0: Compile", "Compile:", "Build ID:", "Log Config:", "Extended message")):
                kind = "diag"
            else:
                kind = "other"
        current = {"id": len(events), "time": timestamp, "kind": kind, "lines": [payload]}
    flush()
    for index, event in enumerate(events):
        event["id"] = index
    return events


def capture_dir(stamp: str) -> Path | None:
    if not STAMP.match(stamp):
        return None
    path = CAPTURES / stamp
    return path if path.is_dir() else None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        return

    def send_json(self, payload: dict | list, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path: Path, content_type: str) -> None:
        if not path.is_file():
            self.send_error(404)
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        if path in {"/", "/index.html"}:
            self.send_file(UI / "index.html", "text/html; charset=utf-8")
            return
        if path == "/app.js":
            self.send_file(UI / "app.js", "text/javascript; charset=utf-8")
            return
        if path == "/style.css":
            self.send_file(UI / "style.css", "text/css; charset=utf-8")
            return
        if path == "/api/live":
            self.send_json(load_cache())
            return
        if path == "/api/captures":
            self.send_json(list_captures())
            return
        if path == "/api/capture":
            self.send_json(dict(CAPTURE_JOB))
            return
        if path.startswith("/api/captures/"):
            stamp = path.removeprefix("/api/captures/").strip("/")
            folder = capture_dir(stamp)
            if folder is None:
                self.send_json({"error": "unknown capture"}, 404)
                return
            qmdl = folder / "diag.qmdl"
            events = qmdl_events(qmdl) if qmdl.is_file() else parse_scat(folder / "scat.log")
            source = "qmdl" if qmdl.is_file() else "scat"
            kind = (query.get("kind") or ["all"])[0]
            q = (query.get("q") or [""])[0].strip().lower()
            try:
                offset = max(0, int((query.get("offset") or ["0"])[0]))
                limit = min(80, max(1, int((query.get("limit") or ["40"])[0])))
            except ValueError:
                offset, limit = 0, 40
            filtered = [
                event for event in events
                if (kind in {"all", event["kind"]})
                and (not q or q in event["summary"].lower() or q in event["text"].lower())
            ]
            page = filtered[offset:offset + limit]
            counts: dict[str, int] = {}
            for event in events:
                counts[event["kind"]] = counts.get(event["kind"], 0) + 1
            self.send_json({
                "id": stamp,
                "notes": parse_notes(folder / "notes.txt"),
                "coverage": read_json(folder / "coverage.json"),
                "timeline": read_timeline(folder / "timeline.csv"),
                "counts": counts,
                "source": source,
                "total": len(filtered),
                "offset": offset,
                "events": [
                    {"id": event["id"], "time": event["time"], "kind": event["kind"], "summary": event["summary"], "text": event["text"]}
                    for event in page
                ],
            })
            return
        self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        if urlparse(self.path).path != "/api/capture":
            self.send_error(404)
            return
        if not start_capture():
            self.send_json({"ok": False, "message": "a dongle read is already running"}, 409)
            return
        self.send_json({"ok": True, "message": "reading the dongle for 20 seconds"})


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"UE trace UI  http://{HOST}:{PORT}/", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
