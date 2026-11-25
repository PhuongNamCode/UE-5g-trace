# Pegatron 5G drop trace

This folder traces the Pegatron FM160 that is on USB now. It does not modify `/home/fcp/5Gmodem` or `/home/fcp/5gmon`.

MobileInsight is the lab capture tool for this Pegatron and for the Quectel RM530. SCAT and CellNinja decode the diagnostic log. The helper below is the SCAT half of that table, plus a ping and registration timeline.

## What is on USB

`detect.sh` names this stick `pegatron_fm160`.

- USB `2cb7:0105`, Fibocom FM160-EAU
- DIAG is `/dev/ttyUSB0` (interface 0, protocol `0x30`). SCAT opens only this port.
- AT is `/dev/ttyUSB1` and `/dev/ttyUSB2`. ModemManager owns `ttyUSB2`. This helper does not open them.
- Data is `cdc_ether`, gateway `192.168.5.1` when the dongle is in router mode.

The APAL Compal (`04b7:9063`) has no DIAG port. Its USB configurations are CDC Ethernet, RNDIS, and MBIM only. MobileInsight, SCAT, and CellNinja do not apply to that stick. If `detect.sh` reports `compal_tributo5g`, this helper exits.

## Coverage rows this run checks

SCAT is started with `-L rrc,nas`. That mask subscribes `0xB80C` and `0xB97F` on every run, and `0xB821` because `rrc` is in the layer list.

| Code | Row | Preliminary Pegatron column | This capture |
| --- | --- | --- | --- |
| `0xB821` | NR RRC: measurement report, reconfiguration, paging, release | 14 packets, version 0.14 | Count of `NR RRC OTA Packet` lines in `scat.log`, each stamped with the host time |
| `0xB80C` | 5GMM state | not seen | Count of `5GMM State:` lines. Zero means the same gap as the table |
| `0xB97F` | NR serving and neighbour RSRP, RSRQ, beams | SCAT text, no timestamps | `NR ML1 Meas Packet` lines with a host timestamp prefix |

SCAT is started through `scat_entry.py`, which keeps the vendored SCAT tree unchanged and adds the lab-table log codes to the LTE/NR mask: `0xB0C0`, `0xB0ED`, `0xB063`, `0xB064`, `0xB18A`, `0xB821`, `0xB80C`, `0xB881`, `0xB888`, `0xB873`, `0xB89B`, `0xB84D`, and `0xB97F`. The raw DIAG file is `diag.qmdl`. `diag.timing.csv` has one row per log packet: host time, code, modem timestamp, and length. `coverage.json` is the same table with a packet count.

[CellNinjia_desktop](vendor/CellNinjia_desktop) is the MAC/RLC decoder for the stats rows. Its source maps message names such as `NR5G MAC UL TB Stats`, `NR5G MAC UCI Information`, and `NR5G RLC DL Stats`. The name-to-diag configuration lives in `lib/libcellninjia_asn1.so`. The checked-in `main.cpp` only asks for three L2 messages, so this tracer subscribes the table codes itself and leaves CellNinjia for decoding those payloads. `decode-nr-12.py` and `oer-diag-timing` are not public repositories; the timing CSV is the local equivalent of that last row.

A code with count 0 was subscribed and the modem did not emit it during the window. On this 5G-only FM160 the LTE rows (`0xB0C0`, `0xB0ED`, `0xB063`, `0xB064`, `0xB18A`) are expected to stay empty, which matches the Pegatron column.

## Run

```bash
python3 trace_drop.py
```

That polls about every 10 seconds until registration leaves `registered`, data leaves `up`, internet loss reaches 100%, the gateway ping fails, or RSRP/SINR collapses. It then keeps logging for 60 seconds. A bounded check that stops even if the link stays up:

```bash
python3 trace_drop.py --duration 60
```

Each run writes `captures/<UTC>/`:

- `timeline.csv` — carrier, IPv4, gateway ping, internet ping, RSRP, SINR, `reg`, `data`
- `scat.log` — SCAT text with a host timestamp on every line
- `drop.pcap` — GSMTAP from SCAT
- `notes.txt` — class, the three code counts, and whether the capture is usable

The web UI allows one session. A poll uses the fresh `5gmon` cache (the same line `signal.sh` prints) and opens the web API only when that cache is stale. A failed or timed-out login is `web=busy`. Direct `--json` reads of this FM160 were timing out while the monitor cache was current, so the timeline follows the cache.

`captures/` is gitignored. The helper never passes `--enforce` and never runs `radio_cycle.py` or `check.sh`. Passwords stay in the 5gmon config and are not copied here.

## Status UI

The viewer reads the 5gmon cache and the files in `captures/`. It does not open DIAG and does not log into the dongle.

```bash
python3 ui_server.py
```

Then open http://127.0.0.1:8765/ . The top row is the current UE status. PHY, RRC, and 5GMM are separate filters, and each measurement stays collapsed until you open it.

## Pause check.sh for a real drop

`/home/fcp/5gmon/check.sh` is on cron every 5 minutes. `RADIO_CYCLE_ON_DOWN=1` sends this Pegatron through airplane mode when status is DOWN, which erases the registration state the trace is trying to see. Pause that job before a drop capture, and put it back after. The helper prints the same commands and does not change the crontab itself.

```bash
crontab -l | grep -v '/home/fcp/5gmon/check.sh' | crontab -
```

Restore:

```bash
(crontab -l 2>/dev/null; echo '*/5 * * * * /home/fcp/5gmon/check.sh') | crontab -
```

Only one program may open `/dev/ttyUSB0`. Leave ModemManager on `ttyUSB2`.

## How a loss is classed

- `usb` — carrier drops or the gateway ping fails
- `registration` — `reg` leaves `registered` (5GMM searching or deregistered) while the gateway ping still works
- `radio` — RSRP falls below -110 dBm or SINR falls below 0 while `reg` is still `registered`
- `data` — `reg` stays `registered` and RSRP stays healthy while internet loss is 100% or `data=down`
- `up` — none of those happened before the trace stopped

## The 22:20 outage was the APAL, and it was a registration loss

`/home/fcp/5gmon/health.log` at 2026-11-24 22:20, Compal Tributo on `192.168.225.1`:

- internet loss 100%, `reg=searching`, RSRP unreadable
- gateway ping still 0% loss, carrier stayed 1
- `check.sh` then ran an op-mode re-attach, and the USB Ethernet name later changed, which is a re-enumeration

That is a registration and data loss with the USB link still up. It is not a Pegatron DIAG sample. `python3 trace_drop.py --self-test` locks that shape to the class `registration`.
