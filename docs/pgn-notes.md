# PGN Notes — B&G / NMEA 2000

## Standard PGNs Supported

| PGN    | Description               | Key Fields                          |
|--------|---------------------------|-------------------------------------|
| 127250 | Vessel Heading            | heading (rad), deviation, variation |
| 128259 | Speed Through Water       | speed (m/s)                         |
| 128267 | Water Depth               | depth (m), offset                   |
| 129025 | Position Rapid Update     | latitude, longitude (1e-7 deg)      |
| 129026 | COG & SOG Rapid Update    | cog (rad), sog (m/s)                |
| 130306 | Wind Data                 | wind speed (m/s), wind angle (rad)  |
| 130310 | Environmental Parameters  | water temperature (K)               |

## Decoding Notes

- All multi-byte integers are **little-endian** (NMEA 2000 spec).
- Angles are in **radians × 10000** (i.e., `int16 / 10000.0` → radians) for most PGNs.
- Speed fields are typically in **0.01 m/s** units (i.e., `uint16 / 100.0` → m/s).
- Temperature is in **0.01 K** units (i.e., `uint16 / 100.0` → Kelvin; subtract 273.15 for Celsius).

## PGN Extraction from 29-bit CAN ID (J1939/N2K)

```
priority     = (arb_id >> 26) & 0x7
reserved     = (arb_id >> 25) & 0x1
data_page    = (arb_id >> 24) & 0x1
pdu_format   = (arb_id >> 16) & 0xFF
pdu_specific = (arb_id >> 8) & 0xFF
src_addr     = arb_id & 0xFF

# PDU2 (broadcast): pdu_format >= 240
pgn = (data_page << 16) | (pdu_format << 8) | pdu_specific

# PDU1 (peer-to-peer): pdu_format < 240
pgn = (data_page << 16) | (pdu_format << 8)
```

## B&G Proprietary PGNs

To be documented as discovered. Proprietary PGNs use the Manufacturer Code
embedded in the data payload (first two bytes after the PGN header).

B&G Manufacturer Code: **0x0069** (105 decimal) — verify against live traffic.

## TODO

- [ ] Capture live CAN traffic and document B&G proprietary PGN structures
- [ ] Verify heading reference field (magnetic vs true) in PGN 127250
- [ ] Document FastPacket reassembly if used (PGN 129029 GNSS Position Data)
