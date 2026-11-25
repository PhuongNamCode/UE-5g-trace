#!/usr/bin/env python3
"""Start SCAT with the coverage-table log codes added to its LTE/NR mask.

The vendored SCAT tree is not modified. This process patches the mask
function, then runs SCAT on the arguments that follow.
"""

from __future__ import annotations

import struct
import sys
from datetime import datetime, timezone
from pathlib import Path

# Item numbers inside equipment id 0xB. These are the rows in the lab table.
TABLE_ITEMS = (
    0x0C0,  # 0xB0C0 LTE RRC
    0x0ED,  # 0xB0ED LTE NAS EMM outgoing
    0x063,  # 0xB063 LTE MAC DL
    0x064,  # 0xB064 LTE MAC UL
    0x18A,  # 0xB18A LTE RLM
    0x821,  # 0xB821 NR RRC
    0x80C,  # 0xB80C 5GMM state
    0x881,  # 0xB881 NR MAC UL stats
    0x888,  # 0xB888 NR MAC DL stats
    0x873,  # 0xB873 NR uplink buffer / BSR
    0x89B,  # 0xB89B NR UCI
    0x84D,  # 0xB84D NR RLC DL stats
    0x97F,  # 0xB97F NR measurements
)

ROWS = (
    ("0xB0C0", "LTE RRC + LTE NAS EMM", "SCAT"),
    ("0xB0ED", "LTE NAS outgoing EMM", "SCAT"),
    ("0xB821", "NR RRC", "SCAT"),
    ("0xB80C", "5GMM state", "SCAT"),
    ("0xB063", "LTE MAC DL", "SCAT"),
    ("0xB064", "LTE MAC UL", "SCAT"),
    ("0xB881", "NR MAC UL stats", "CellNinjia"),
    ("0xB888", "NR MAC DL stats", "CellNinjia"),
    ("0xB873", "NR uplink buffer", "CellNinjia"),
    ("0xB89B", "NR UCI", "CellNinjia"),
    ("0xB84D", "NR RLC DL stats", "CellNinjia"),
    ("0xB97F", "NR measurements", "SCAT"),
    ("0xB18A", "LTE RLM", "MobileInsight"),
)


def _or_items(mask: bytes) -> bytes:
    if len(mask) < 16:
        return mask
    _cmd, _op, _equip, last = struct.unpack_from("<LLLL", mask, 0)
    payload = bytearray(mask[16:])
    for bit in TABLE_ITEMS:
        if bit > last or bit // 8 >= len(payload):
            continue
        payload[bit // 8] |= 1 << (bit % 8)
    return mask[:16] + bytes(payload)


def install() -> None:
    import scat.parsers.qualcomm.diagcmd as diagcmd
    import scat.writers.rawwriter as rawwriter
    import scat.writers as writers

    original = diagcmd.log_mask_scat_lte

    def wider(num_max_items=0x09FF, layers=None):
        return _or_items(original(num_max_items, [] if layers is None else layers))

    diagcmd.log_mask_scat_lte = wider

    class TimingRawWriter(rawwriter.RawWriter):
        def __init__(self, fname: str, header: bytes = b"", trailer: bytes = b""):
            super().__init__(fname, header, trailer)
            self._timing_path = str(Path(fname).with_suffix(".timing.csv"))
            self._timing = open(self._timing_path, "w", encoding="utf-8")
            self._timing.write("host_time,log_code,modem_ts,length\n")

        def write_cp(self, sock_content: bytes, radio_id: int = 0, ts=None):
            super().write_cp(sock_content, radio_id, ts)
            host = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            for code, modem, length in log_records(sock_content):
                self._timing.write(f"{host},0x{code:04X},{modem},{length}\n")
            self._timing.flush()

        def __exit__(self, exc_type, exc_value, traceback):
            self._timing.close()
            return super().__exit__(exc_type, exc_value, traceback)

    rawwriter.RawWriter = TimingRawWriter
    writers.RawWriter = TimingRawWriter


CODE_KIND = {
    0xB97F: "phy",
    0xB821: "rrc",
    0xB822: "rrc",
    0xB823: "rrc",
    0xB80C: "nas",
    0xB800: "nas",
    0xB801: "nas",
    0xB881: "mac-ul",
    0xB888: "mac-dl",
    0xB873: "bsr",
    0xB89B: "uci",
    0xB84D: "rlc",
    0xB0C0: "lte",
    0xB0ED: "lte",
    0xB063: "lte",
    0xB064: "lte",
    0xB18A: "lte",
}

CODE_NAME = {int(code, 16): name for code, name, _decoder in (
    ("0xB0C0", "LTE RRC", ""),
    ("0xB0ED", "LTE NAS EMM", ""),
    ("0xB821", "NR RRC", ""),
    ("0xB80C", "5GMM state", ""),
    ("0xB063", "LTE MAC DL", ""),
    ("0xB064", "LTE MAC UL", ""),
    ("0xB881", "NR MAC UL stats", ""),
    ("0xB888", "NR MAC DL stats", ""),
    ("0xB873", "NR uplink buffer", ""),
    ("0xB89B", "NR UCI", ""),
    ("0xB84D", "NR RLC DL stats", ""),
    ("0xB97F", "NR measurements", ""),
    ("0xB18A", "LTE RLM", ""),
)}


def unescape(data: bytes) -> bytes:
    out = bytearray()
    index = 0
    while index < len(data):
        if data[index] == 0x7D and index + 1 < len(data):
            out.append(data[index + 1] ^ 0x20)
            index += 2
        else:
            out.append(data[index])
            index += 1
    return bytes(out)


def log_records(frame: bytes):
    """Yield (log_code, modem_timestamp, length) from one HDLC frame.

    A Qualcomm log item inside the frame is
    0x10 0x00, length, the same length again, then the log code.
    """
    body = unescape(frame.split(b"\x7e", 1)[0])
    index = 0
    while True:
        found = body.find(b"\x10\x00", index)
        if found < 0 or found + 14 > len(body):
            return
        first, second, code = struct.unpack_from("<HHH", body, found + 2)
        if first == second and 8 < first < 4000 and (code & 0xF000) == 0xB000:
            modem = struct.unpack_from("<Q", body, found + 8)[0]
            yield code, modem, first
        index = found + 2


def read_qmdl(path: Path) -> list[dict]:
    """One UI row per diagnostic log item in a QMDL file."""
    data = path.read_bytes()
    events = []
    for raw in data.split(b"\x7e"):
        if len(raw) < 8:
            continue
        body = unescape(raw)
        index = 0
        while True:
            found = body.find(b"\x10\x00", index)
            if found < 0 or found + 16 > len(body):
                break
            first, second, code = struct.unpack_from("<HHH", body, found + 2)
            if first == second and 8 < first < 4000 and (code & 0xF000) == 0xB000:
                modem = struct.unpack_from("<Q", body, found + 8)[0]
                payload = body[found + 16:found + 16 + max(0, min(first - 12, 24))]
                version = struct.unpack_from("<I", payload, 0)[0] if len(payload) >= 4 else None
                name = CODE_NAME.get(code, "log")
                summary = f"{name}  {first} B"
                if version is not None and code == 0xB821:
                    summary = f"NR RRC  header 0x{version:x}  {first} B"
                events.append({
                    "id": len(events),
                    "time": f"modem {modem}",
                    "kind": CODE_KIND.get(code, "other"),
                    "code": f"0x{code:04X}",
                    "summary": summary,
                    "text": payload.hex(" ") if payload else "",
                })
            index = found + 2
    return events


def main() -> None:
    install()
    from scat.main import scat_main

    scat_main()


if __name__ == "__main__":
    main()
