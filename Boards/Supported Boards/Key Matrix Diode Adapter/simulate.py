#!/usr/bin/env python3
"""ngspice simulation of the full matrix through the diode adapter.

Every row and column is modelled from the RP2040 pin to the switch:

  RP2040 pin   internal pull-up (50k or 80k), 5 pF pin capacitance, and for rows
               the 40 ohm output driver that pulls the scanned row low
  ribbon       the 20-pin IDC cable to the adapter, as an LC ladder
               (about 0.7 uH/m and 50 pF/m per conductor)
  adapter PCB  copper capacitance of each net, measured from keymatrix-adapter.kicad_pcb
               (track length, vias and pads)
  diodes       one 1N4148W per switch on the adapter, cathode to the row
  harness      each switch's own wire (switch to its R terminal) and the column
               common wire daisy-chained through the four switches of a column,
               as an LC ladder with the switches tapped along it

The scan follows the firmware: pull a row low, wait the settle time (5 us),
read the columns, release the row, wait the row delay (50 us by default, set in
the web config), then the next row.

For each wiring length it reports:
  - how long a press takes to show at the RP2040 pin (must fit in the settle time)
  - how long the columns take to recover after a row is released (the row
    delay must cover this)
  - whether every read is right with the default timing, for several held-switch
    patterns (pressed < 0.8 V, open > 2.0 V on every switch)

Usage: python3 simulate.py   (needs ngspice, and KiCad's Python module for the PCB numbers)
       python3 simulate.py --trace trace.json   replays a firmware GPIO trace (see run_trace)
"""

import itertools
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import generate  # noqa: E402

VDD, VIL, VIH = 3.3, 0.8, 2.0
ROWS, COLS = len(generate.ROW_GPIOS), len(generate.COL_GPIOS)
SETTLE_US = 5          # KEY_MATRIX_SETTLE_MICROS
ROW_DELAY_US = 50      # KEY_MATRIX_ROW_DELAY_MICROS
CODE_US = 1.0          # loop overhead per row (GPIO calls, reads), rough
RON = 40               # RP2040 output low resistance
PIN_C = 5e-12          # RP2040 pin capacitance
RIBBON_L, RIBBON_C = 0.7e-6, 50e-12     # per metre, per conductor
WIRE_L, WIRE_C = 1.0e-6, 50e-12         # hookup wire in a bundle, per metre
SECTIONS = 4           # LC sections per cable
PCB_C_PER_MM = 0.12e-12  # 0.3 mm track over ground pour, 1.6 mm FR4 (conservative)
VIA_C, PAD_C = 0.5e-12, 0.8e-12
DIODE_MODEL = ".model D1N4148 D(Is=2.52n Rs=0.568 N=1.752 Cjo=4p M=0.4 Vj=0.5 tt=20n Bv=100 Ibv=100u)"

# (name, ribbon m, each switch wire m, column common chain m)
WIRING = [
    ("no wiring (pins only)", 0.0, 0.0, 0.0),
    ("short: bench test", 0.15, 0.2, 1.0),
    ("typical: arcade stick", 0.3, 0.5, 3.0),
    ("long: cabinet", 1.0, 1.0, 6.0),
]
PULLUPS = [50e3, 80e3]

SCENARIOS = {
    "single switch": {(0, 0)},
    "ghost rectangle": {(0, 0), (0, 1), (1, 0)},
    "whole row": {(0, c) for c in range(COLS)},
    "whole column": {(r, 0) for r in range(ROWS)},
    "everything held": set(itertools.product(range(ROWS), range(COLS))),
    "checkerboard": {(r, c) for r in range(ROWS) for c in range(COLS) if (r + c) % 2 == 0},
    # held on every other row: each held row is followed by an open one, the
    # worst case for a column recovering in time
    "every other row held": {(r, c) for r in range(0, ROWS, 2) for c in range(COLS)},
}


def pcb_caps():
    """Copper capacitance per net on the adapter PCB."""
    try:
        import pcbnew
    except ImportError:
        return {}, "PCB not read (no KiCad Python module): using 10 pF per net"
    board = pcbnew.LoadBoard(os.path.join(HERE, generate.NAME + ".kicad_pcb"))
    caps = {}
    for t in board.GetTracks():
        n = t.GetNetname()
        if isinstance(t, pcbnew.PCB_VIA):
            caps[n] = caps.get(n, 0) + VIA_C
        else:
            caps[n] = caps.get(n, 0) + pcbnew.ToMM(t.GetLength()) * PCB_C_PER_MM
    for fp in board.GetFootprints():
        for p in fp.Pads():
            n = p.GetNetname()
            caps[n] = caps.get(n, 0) + PAD_C
    return caps, None


def ladder(lines, name, a, b, length, l_per_m, c_per_m):
    """LC ladder from node a to node b. Returns nothing; b exists afterwards."""
    if length <= 0:
        lines.append(f"R{name} {a} {b} 0.01")
        return
    L = l_per_m * length / SECTIONS
    C = c_per_m * length / SECTIONS
    prev = a
    for k in range(SECTIONS):
        node = b if k == SECTIONS - 1 else f"{name}_{k}"
        lines.append(f"L{name}_{k} {prev} {node} {L:.4g}")
        lines.append(f"RL{name}_{k} {prev} {node} 1k")  # damping, like real wire losses
        lines.append(f"C{name}_{k} {node} 0 {C:.4g}")
        prev = node


def scan_intervals(gap_us, cycles):
    """The firmware's fixed scan: row r low for the settle time, then the gap."""
    t = SETTLE_US * 1e-6
    w = t + gap_us * 1e-6
    lows = {r: [((cyc * ROWS + r) * w, (cyc * ROWS + r) * w + t) for cyc in range(cycles)] for r in range(ROWS)}
    return lows, cycles * ROWS * w


def netlist(pressed, wiring, pullup, caps, lows, end):
    """lows: row -> list of (start, end) seconds when the firmware drives it low."""
    _, ribbon, sw_wire, chain = wiring
    L = [DIODE_MODEL, "VDD vdd 0 DC 3.3", f".model SWG SW(Ron={RON} Roff=1e12 Vt=0.5 Vh=0)",
         ".model SWK SW(Ron=0.1 Roff=1e12 Vt=0.5 Vh=0)", "VON on 0 DC 1"]
    for r in range(ROWS):
        rn = generate.row_net(r)
        L += [f"RPU_r{r} vdd rm{r} {pullup}", f"CP_r{r} rm{r} 0 {PIN_C}"]
        pwl = ["0 0"]
        for a, b in sorted(lows.get(r, [])):
            pwl += [f"{a + 1e-9:.12g} 0", f"{a + 2e-9:.12g} 1", f"{b - 1e-9:.12g} 1", f"{b:.12g} 0"]
        L += [f"VD{r} d{r} 0 PWL({' '.join(pwl)})", f"SD{r} rm{r} 0 d{r} 0 SWG"]
        ladder(L, f"rr{r}", f"rm{r}", f"ra{r}", ribbon, RIBBON_L, RIBBON_C)
        L.append(f"CPCB_r{r} ra{r} 0 {caps.get(rn, 10e-12):.4g}")
    for c in range(COLS):
        cn = generate.col_net(c)
        L += [f"RPU_c{c} vdd cm{c} {pullup}", f"CP_c{c} cm{c} 0 {PIN_C}"]
        ladder(L, f"rc{c}", f"cm{c}", f"ca{c}", ribbon, RIBBON_L, RIBBON_C)
        L.append(f"CPCB_c{c} ca{c} 0 {caps.get(cn, 10e-12):.4g}")
        # column common daisy-chained from the B terminal through the column's switches
        prev = f"ca{c}"
        for r in range(ROWS):
            ladder(L, f"ch{c}_{r}", prev, f"ch{c}t{r}", chain / ROWS, WIRE_L, WIRE_C)
            prev = f"ch{c}t{r}"
    for r, c in itertools.product(range(ROWS), range(COLS)):
        sn = generate.sw_net(r, c)
        # adapter side of the switch: diode anode + PCB copper, then the switch wire
        L.append(f"CPCB_s{r}{c} sa{r}_{c} 0 {caps.get(sn, 3e-12):.4g}")
        L.append(f"D{r}_{c} sa{r}_{c} ra{r} D1N4148")
        ladder(L, f"sw{r}_{c}", f"sa{r}_{c}", f"sk{r}_{c}", sw_wire, WIRE_L, WIRE_C)
        if (r, c) in pressed:
            L.append(f"SK{r}_{c} sk{r}_{c} ch{c}t{r} on 0 SWK")
        else:
            L.append(f"RK{r}_{c} sk{r}_{c} ch{c}t{r} 1e12")
    L += [f".tran 20n {end:.6g} 0 0.25u", ".options method=gear", ".control", "run"]
    return L


def run_ngspice(L):
    with tempfile.NamedTemporaryFile("w", suffix=".cir", delete=False) as f:
        f.write("* adapter matrix\n" + "\n".join(L))
        path = f.name
    try:
        return subprocess.run(["ngspice", "-b", path], capture_output=True, text=True).stdout
    finally:
        os.unlink(path)


def simulate(pressed, wiring, pullup, caps, gap_us, cycles=2):
    """Column readings in the last scan, recovery times, and press detection times."""
    t = SETTLE_US * 1e-6
    w = t + gap_us * 1e-6
    lows, end = scan_intervals(gap_us, cycles)
    L = netlist(pressed, wiring, pullup, caps, lows, end)
    meas = []
    base = cycles - 1  # last pass: with 2 passes every row follows a scanned row
    for cyc in (base,):
        for r in range(ROWS):
            s = (cyc * ROWS + r) * w
            for c in range(COLS):
                meas.append(f"meas tran v_{r}_{c} FIND v(cm{c}) AT={s + t - 5e-9:.12g}")
                if (r, c) in pressed:
                    meas.append(f"meas tran det_{r}_{c} WHEN v(cm{c})={VIL} FALL=1 FROM={s:.12g}")
                    meas.append(f"meas tran rec_{r}_{c} WHEN v(cm{c})={VIH} RISE=1 FROM={s + t:.12g}")
    L += meas + [".endc", ".end"]
    out = run_ngspice(L)
    vals = {}
    for m in re.finditer(r"^(v|det|rec)_(\d+)_(\d+)\s*=\s*([-+0-9.eE]+)", out, re.M):
        vals[(m.group(1), int(m.group(2)), int(m.group(3)))] = float(m.group(4))
    if sum(1 for k in vals if k[0] == "v") != ROWS * COLS:
        sys.exit("ngspice did not return every reading:\n" + out[-3000:])
    reads = {(r, c): vals[("v", r, c)] for r in range(ROWS) for c in range(COLS)}
    det = max([vals[k] - (base * ROWS + k[1]) * w for k in vals if k[0] == "det"], default=0.0)
    rec = max([vals[k] - ((base * ROWS + k[1]) * w + t) for k in vals if k[0] == "rec"], default=0.0)
    return reads, det * 1e6, rec * 1e6


def run_trace(path):
    """Replay a GPIO trace recorded from the firmware's scan loop.

    Input JSON:
      {"wiring": "typical",            # name prefix from WIRING (default "typical")
       "pullup": 50000,                # RP2040 pull-up in ohms (default 50000)
       "pressed": [[row, col], ...],   # 0-based, rows = ROW_GPIOS order, cols = COL_GPIOS order
       "events": [{"t_us": 0, "gpio": 16, "drive": "low"},       # firmware drives a row low
                  {"t_us": 5, "read": true},                     # firmware reads the columns
                  {"t_us": 5.2, "gpio": 16, "drive": "release"}, # row back to input (pull-up)
                  ...]}
    Output JSON on stdout: one entry per read with each column's voltage at the
    RP2040 pin and its logic level (1, 0, or null between VIL and VIH)."""
    import json
    spec = json.load(open(path))
    wname = spec.get("wiring", "typical")
    wiring = next(w for w in WIRING if w[0].startswith(wname))
    pullup = float(spec.get("pullup", 50e3))
    pressed = {tuple(p) for p in spec.get("pressed", [])}
    lows, start, reads = {}, {}, []
    for ev in sorted(spec["events"], key=lambda e: e["t_us"]):
        t = ev["t_us"] * 1e-6
        if ev.get("read"):
            reads.append(t)
        elif "gpio" in ev:
            r = generate.ROW_GPIOS.index(ev["gpio"])
            if ev["drive"] == "low":
                start.setdefault(r, t)
            elif r in start:
                lows.setdefault(r, []).append((start.pop(r), t))
    end = max([e["t_us"] for e in spec["events"]]) * 1e-6 + 1e-6
    for r, a in start.items():
        lows.setdefault(r, []).append((a, end))
    caps, _ = pcb_caps()
    L = netlist(pressed, wiring, pullup, caps, lows, end)
    for i, t in enumerate(reads):
        for c in range(COLS):
            L.append(f"meas tran v_{i}_{c} FIND v(cm{c}) AT={max(t, 1e-9):.12g}")
    out = run_ngspice(L + [".endc", ".end"])
    vals = {(int(m.group(1)), int(m.group(2))): float(m.group(3))
            for m in re.finditer(r"^v_(\d+)_(\d+)\s*=\s*([-+0-9.eE]+)", out, re.M)}
    if len(vals) != len(reads) * COLS:
        sys.exit("ngspice did not return every reading:\n" + out[-3000:])
    level = lambda v: 1 if v >= VIH else 0 if v <= VIL else None
    result = [{"t_us": round(t * 1e6, 4),
               "columns": {str(g): {"v": round(vals[(i, c)], 4), "level": level(vals[(i, c)])}
                           for c, g in enumerate(generate.COL_GPIOS)}}
              for i, t in enumerate(reads)]
    json.dump({"wiring": wiring[0], "pullup": pullup, "reads": result}, sys.stdout, indent=1)
    print()


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--trace":
        return run_trace(sys.argv[2])
    caps, note = pcb_caps()
    if note:
        print(note)
    else:
        rows = [caps.get(generate.row_net(r), 0) for r in range(ROWS)]
        cols = [caps.get(generate.col_net(c), 0) for c in range(COLS)]
        print(f"Adapter PCB copper per net: rows {min(rows) * 1e12:.1f}-{max(rows) * 1e12:.1f} pF, "
              f"columns {min(cols) * 1e12:.1f}-{max(cols) * 1e12:.1f} pF")
    scan_us = ROWS * (SETTLE_US + ROW_DELAY_US + CODE_US)
    print(f"Timing: settle {SETTLE_US} us, row delay {ROW_DELAY_US} us -> full scan about {scan_us:.0f} us "
          f"(USB reports every 1000 us)")
    print(f"Pass = pressed reads < {VIL} V and open reads > {VIH} V on all {ROWS * COLS} switches, every pattern\n")
    print(f"{'wiring (ribbon / switch wire / column chain)':<46} {'pull-up':>7} {'press seen':>10} "
          f"{'recovery':>9} {'worst pressed':>13} {'worst open':>10}  result")
    ok = True
    for wiring, pullup in itertools.product(WIRING, PULLUPS):
        # timing of one press and one release, measured with a long gap
        _, det, rec = simulate(SCENARIOS["every other row held"], wiring, pullup, caps, 100.0, cycles=1)
        worst_low, worst_high, fails = 0.0, VDD, []
        for name, pressed in SCENARIOS.items():
            reads, _, _ = simulate(pressed, wiring, pullup, caps, ROW_DELAY_US)
            for (r, c), v in reads.items():
                if (r, c) in pressed:
                    worst_low = max(worst_low, v)
                    if v > VIL:
                        fails.append(f"{name}: R{r + 1}C{c + 1} pressed read {v:.2f} V")
                else:
                    worst_high = min(worst_high, v)
                    if v < VIH:
                        fails.append(f"{name}: R{r + 1}C{c + 1} open read {v:.2f} V")
        if det > SETTLE_US:
            fails.append(f"press takes {det:.2f} us to show, longer than the settle time")
        ok &= not fails
        label = wiring[0] if wiring[1] == 0 else f"{wiring[0]} ({wiring[1]:g} / {wiring[2]:g} / {wiring[3]:g} m)"
        print(f"{label:<46} {pullup / 1e3:>5.0f}k {det * 1e3:>7.0f} ns {rec:>6.1f} us {worst_low:>11.3f} V "
              f"{worst_high:>8.3f} V  {'PASS' if not fails else 'FAIL'}" + ("" if not fails else f"  {fails[0]}"))
    print("\npress seen: time from the row going low until a pressed column reads low at the RP2040 pin")
    print("recovery:   time after a row is released until its columns read high again (row delay must cover it)")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
