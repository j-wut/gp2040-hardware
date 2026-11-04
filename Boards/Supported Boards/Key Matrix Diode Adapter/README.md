# Key matrix diode adapter

A board that turns the 20-pin header of the **RP2040 Advanced Breakout Board (USB Passthrough edition)** into 44
matrix switch inputs with full N-key rollover: all 15 button pins as 4 rows x 11 columns. It goes with the
`RP2040AdvancedBreakoutBoardUSBPassthroughMatrix` firmware config in
[j-wut/GP2040-CE](https://github.com/j-wut/GP2040-CE) (keyboard matrix support).

- 103 x 101.6 mm, 2 layers, 0.2 mm tracks, all through-hole connectors plus 44 SOD-123 diodes.
- **J1** connects to the breakout board's 20-pin header with a straight 20-pin IDC ribbon cable (pin 1 to pin 1).
- **J2-J5** are two pairs of 20-pin switch connectors (A and B).
- **J6-J13** are screw terminals all around the edge: two per switch, in the same order as the connector pins. The
  last 4 switches are on screw terminals only.
- One 1N4148W diode per switch, already on the board. No diodes are needed in the wiring.

## How a switch is wired

Every switch has a **slot** with two contacts, A and B. Wire the switch between them, either on the connector pair
(pin N on A and pin N on B) or on its two neighbouring screw terminals. Both reach the same switch. A switch has no
polarity, so either wire can go to A.

- **A** goes through the switch's diode to its row.
- **B** is its column. All the B contacts of one column are connected on the board, so a harness can share them.
  A contacts must never be shared.

Do not plug a normal 20-pin button harness (buttons wired to ground) into the switch connectors.

## Slot order

Both pairs have the same order. **Pair 1 follows the breakout's 20-pin header.** Each of its 15 button pins
carries the switch that defaults to that pin's button, so a harness made for the breakout's pinout lines up:

- Pin 5 is K4 (L2), pin 13 is P1 (B3), pin 20 is U (Up), and so on.

The breakout's 5 power and ground positions (pins 1-4 and 9) carry five more switches. Pair 2 carries the next
20 in row order, and the last 4 are on screw terminals only. R*n*C*m* is the switch in row *n*, column *m*, as listed in the web config (Add-ons > Keyboard
Matrix).

| Pin | Pair 1 (J2 A / J3 B) | Pair 2 (J4 A / J5 B) |
|-----|-----|-----|
| 1 | R2C5 | R2C10 |
| 2 | R2C6 | R2C11 |
| 3 | R2C7 | R3C1 |
| 4 | R2C8 | R3C2 |
| 5 | K4 (R1C1) | R3C3 |
| 6 | K3 (R1C2) | R3C4 |
| 7 | K2 (R1C3) | R3C5 |
| 8 | K1 (R1C4) | R3C6 |
| 9 | R2C9 | R3C7 |
| 10 | P4 (R1C5) | R3C8 |
| 11 | P3 (R1C6) | R3C9 |
| 12 | P2 (R1C7) | R3C10 |
| 13 | P1 (R1C8) | R3C11 |
| 14 | S2 (R1C9) | R4C1 |
| 15 | A1 (R1C10) | R4C2 |
| 16 | S1 (R1C11) | R4C3 |
| 17 | L (R2C1) | R4C4 |
| 18 | R (R2C2) | R4C5 |
| 19 | D (R2C3) | R4C6 |
| 20 | U (R2C4) | R4C7 |

Screw terminals only: R4C8, R4C9, R4C10, R4C11.

The screw terminals hold the same slots in the same order, A then B:

- pair 1 along the top edge (pins 1-12, left to right), then down the right edge (pins 13-20, top to bottom)
- pair 2 along the bottom edge (pins 1-12, left to right), then down the left edge (pins 13-20, top to bottom)
- the terminal-only switches at the lower end of each side: R4C8 and R4C9 on the right, R4C10 and R4C11 on the left

Each slot is labelled on the silkscreen.

GPIO 20-22 (A2, L3, R3 on the breakout's 4-pin harness) are not on the 20-pin header and stay ordinary buttons on
the breakout board. Don't wire anything else to the breakout's own K1-K4, P1-P4, S1, S2, A1, L, R, D, U terminals
while the adapter is connected: those GPIOs are matrix lines now.

## Signals

| J1 pin | Signal | Breakout label | | J1 pin | Signal | Breakout label |
|-----|--------|----|-|-----|--------|----|
| 1 | +5V | VCC | | 2 | +5V | VCC |
| 3 | GND | G | | 4 | GND | G |
| 5 | COL1 (GP5) | K4 | | 6 | COL2 (GP6) | K3 |
| 7 | COL3 (GP7) | K2 | | 8 | COL4 (GP8) | K1 |
| 9 | GND | G | | 10 | COL5 (GP9) | P4 |
| 11 | COL6 (GP10) | P3 | | 12 | COL7 (GP11) | P2 |
| 13 | COL8 (GP12) | P1 | | 14 | COL9 (GP13) | S2 |
| 15 | COL10 (GP14) | A1 | | 16 | COL11 (GP15) | S1 |
| 17 | ROW1 (GP16) | L | | 18 | ROW2 (GP17) | R |
| 19 | ROW3 (GP18) | D | | 20 | ROW4 (GP19) | U |

The pin numbers are the pad numbers of the 20-pin header on the breakout board's own design file (v5.6, in this
repository): pins 1 and 2 are +5 V, pin 20 is UP. `check.py` reads that file and checks J1 against it. Check pin 1
on your board before the first power-up anyway.

## Files

| File | What it is |
|------|------------|
| `keymatrix-adapter.kicad_sch`, `.kicad_pcb`, `.kicad_pro` | KiCad 7 project |
| `fab/gerbers/` | Gerbers and drill file for ordering the PCB |
| `fab/keymatrix-adapter-bom.csv` | Bill of materials |
| `fab/keymatrix-adapter-pos.csv` | Pick-and-place positions for the diodes |
| `fab/keymatrix-adapter-schematic.pdf` | Schematic |
| `generate.py`, `autoroute.py` | Build the schematic and the routed PCB from one parts and nets table |
| `check.py` | Runs the checks below and writes `fab/` |
| `simulate.py`, `sim-results.txt` | ngspice simulation of the full matrix through this board, and its results |

## Checks

`python3 generate.py && python3 check.py [path/to/GP2040-CE]` (needs KiCad 7 with its Python module) regenerates the
design and checks:

- J1's pin numbers match the 20-pin header on the breakout board's design file in this repository.
- KiCad DRC: no clearance or other violations, no unconnected pads.
- The schematic netlist (from `kicad-cli`) connects exactly the same pads as the PCB.
- Every one of the 44 switches has its own diode, cathode on the right row. Given a GP2040-CE checkout, the rows and
  columns must also match `KEY_MATRIX_ROW_PINS` / `KEY_MATRIX_COL_PINS` in the firmware board config.
- Every switch is on exactly one connector slot. Pair 1 is in breakout pin order. The screw terminals, read around
  the board, are in the same slot order as the connectors, with the terminal-only switches in order between them.

## Simulation

`python3 simulate.py` runs ngspice on the whole 4 x 11 matrix through this board: RP2040 pull-ups and pins, the ribbon
cable, the copper of every net on this PCB (measured from the board file), the 44 diodes, and the harness wiring
(each switch's own wire plus the column's B wire daisy-chained through its four switches). It scans the way the firmware
does and checks every read for several held-switch patterns, including all 44 held. Results:
[sim-results.txt](sim-results.txt).

| Wiring (ribbon / switch wire / column chain) | Press seen at the pin | Column recovery | Result with defaults |
|-----|-----|-----|-----|
| short: 0.15 m / 0.2 m / 1 m | 0.2 us | up to 10 us | pass |
| typical arcade stick: 0.3 m / 0.5 m / 3 m | 0.3 us | up to 19 us | pass |
| long cabinet: 1 m / 1 m / 6 m | 0.6 us | up to 34 us | pass |

Wire length adds no meaningful latency: a press shows up at the pin in under 0.6 us even with cabinet
wiring. What wiring does set is how long a released row's columns take to climb back through the RP2040's weak
pull-ups. The firmware waits a fixed **row delay** after each row for that (default 50 us, set in the web config
next to the settle time). With the defaults a full scan of all four rows takes about 224 us, well inside the 1 ms
between USB reports. Shorter wiring can use a smaller row delay; see `docs/keymatrix-sim/results.txt` in the firmware repository.

Not built or tested on hardware yet.
