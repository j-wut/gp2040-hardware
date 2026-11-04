#!/usr/bin/env python3
"""Check the adapter design and write the manufacturing files.

1. KiCad DRC on the PCB (clearances, unconnected pads, ...). Must be clean.
2. The schematic netlist (exported by kicad-cli) must connect exactly the same
   pads as the PCB.
3. Every switch has its own diode on the right row, and the connector and
   screw terminal order is right. With a GP2040-CE checkout given
   (python3 check.py path/to/GP2040-CE), the rows and columns must also match
   the firmware board config.
4. Writes gerbers, drill, pick-and-place, BOM and a schematic PDF to fab/.
"""

import csv
import json
import os
import re
import subprocess
import sys

import pcbnew

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import generate  # noqa: E402

NAME = generate.NAME
PCB = os.path.join(HERE, NAME + ".kicad_pcb")
SCH = os.path.join(HERE, NAME + ".kicad_sch")
BUILD = os.path.join(HERE, "build")
FAB = os.path.join(HERE, "fab")
# optional GP2040-CE checkout, to compare against the firmware board config
FIRMWARE = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("GP2040_CE")
BOARD_CONFIG = ("configs", "RP2040AdvancedBreakoutBoardUSBPassthroughMatrix", "BoardConfig.h")

errors = []


def fail(msg):
    errors.append(msg)
    print("FAIL:", msg)


def drc():
    os.makedirs(BUILD, exist_ok=True)
    report = os.path.join(BUILD, "drc.rpt")
    board = pcbnew.LoadBoard(PCB)
    pcbnew.WriteDRCReport(board, report, pcbnew.EDA_UNITS_MILLIMETRES, True)
    text = open(report).read()
    # Library-table warnings only mean this script runs without a KiCad
    # user config; the footprints are embedded in the board file.
    kinds = re.findall(r"^\[(\w+)\]:", text, re.M)
    real = [k for k in kinds if k != "lib_footprint_issues"]
    unconnected = int(re.search(r"\*\* Found (\d+) unconnected pads", text).group(1))
    print(f"DRC: {len(real)} violations, {unconnected} unconnected pads")
    if real:
        fail(f"DRC found {len(real)} violations: {sorted(set(real))} (see build/drc.rpt)")
    if unconnected:
        fail(f"DRC found {unconnected} unconnected pads")


# the breakout board's own source file, in this repository
BREAKOUT_SOURCE = os.path.join(
    HERE, "..", "..", "GP2040-CE Official Boards", "RP2040 Advanced Breakout Board",
    "RP2040 Advanced Breakout Board - Passthrough", "Source files",
    "PCB - RP2040 Advanced Breakout Board - Version 5.6 - PT.json")
# its net names for the header pins -> the signal names used here
BREAKOUT_NETS = {"UP": "GP19", "DOWN": "GP18", "RIGHT": "GP17", "LEFT": "GP16", "S1": "GP15",
                 "A1": "GP14", "S2": "GP13", "P1": "GP12", "P2": "GP11", "P3": "GP10", "P4": "GP9",
                 "K1": "GP8", "K2": "GP7", "K3": "GP6", "K4": "GP5", "GND": "GND", "+5V": "+5V"}


def check_breakout_header():
    """The adapter's header map must match the 20-pin header footprint on the
    breakout board itself (EasyEDA source: pad number and net of each pad)."""
    if not os.path.exists(BREAKOUT_SOURCE):
        fail(f"breakout board source not found: {BREAKOUT_SOURCE}")
        return
    shapes = json.load(open(BREAKOUT_SOURCE))["shape"]
    headers = []
    for shape in shapes:
        parts = shape.split("#@$")
        if parts[0].startswith("LIB~") and "HDR-TH_20P" in parts[0]:
            pads = [p.split("~") for p in parts if p.startswith("PAD~")]
            headers.append({int(f[8]): f[7] for f in pads})
    if len(headers) != 1:
        fail(f"expected one 20-pin header on the breakout board, found {len(headers)}")
        return
    pins = {pin: BREAKOUT_NETS.get(net, net) for pin, net in headers[0].items()}
    if pins != generate.HEADER:
        fail(f"header map differs from the breakout board: {pins}")
    else:
        print("Breakout header: J1 pin numbers match the breakout board's 20-pin header")


def pcb_nets():
    board = pcbnew.LoadBoard(PCB)
    groups = {}
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            if pad.GetNetname():
                groups.setdefault(pad.GetNetname(), set()).add((fp.GetReference(), pad.GetNumber()))
    return groups


def sch_nets():
    out = os.path.join(BUILD, NAME + ".net")
    subprocess.run(["kicad-cli", "sch", "export", "netlist", "--output", out, SCH],
                   check=True, stdout=subprocess.DEVNULL)
    text = open(out).read()
    groups = {}
    for block in text.split("(net (code ")[1:]:
        name = re.search(r'\(name "([^"]+)"\)', block).group(1).lstrip("/")
        groups[name] = set(re.findall(r'\(node \(ref "([^"]+)"\) \(pin "([^"]+)"\)', block))
    return groups


def compare_netlists():
    pcb = pcb_nets()
    sch = {k: v for k, v in sch_nets().items() if not k.startswith("unconnected-")}
    pcb_sets = sorted(sorted(v) for v in pcb.values())
    sch_sets = sorted(sorted(v) for v in sch.values() if len(v) > 0)
    # single-pin nets in the schematic are unconnected pins; the PCB has none
    sch_sets = [s for s in sch_sets if len(s) > 1 or s in pcb_sets]
    if pcb_sets != sch_sets:
        only_pcb = [s for s in pcb_sets if s not in sch_sets]
        only_sch = [s for s in sch_sets if s not in pcb_sets]
        fail(f"schematic and PCB netlists differ: pcb-only {only_pcb[:3]} sch-only {only_sch[:3]}")
    else:
        print(f"Netlist: schematic and PCB match ({len(pcb_sets)} nets)")
    return pcb


def check_firmware(pcb):
    rows, cols = generate.ROW_GPIOS, generate.COL_GPIOS
    if FIRMWARE:
        cfg = open(os.path.join(FIRMWARE, *BOARD_CONFIG)).read()
        fw_rows = [int(v) for v in re.search(r"#define KEY_MATRIX_ROW_PINS ([\d, ]+)", cfg).group(1).split(",")]
        fw_cols = [int(v) for v in re.search(r"#define KEY_MATRIX_COL_PINS ([\d, ]+)", cfg).group(1).split(",")]
        if fw_rows != rows or fw_cols != cols:
            fail(f"board config rows {fw_rows} cols {fw_cols} differ from the adapter")
        else:
            print("Firmware board config: rows and columns match")
    else:
        print("Firmware board config: not compared (pass a GP2040-CE checkout to compare)")
    # every header pin carries the right GPIO, and every switch has its diode
    # from its own terminal to its row, cathode on the row
    header = {pin: net for net, nodes in pcb.items() for ref, pin in nodes if ref == "J1"}
    for pin, sig in generate.HEADER.items():
        want = generate.header_net(pin)
        if header.get(str(pin)) != want:
            fail(f"J1 pin {pin} is {header.get(str(pin))}, expected {want} ({sig})")
    # switch k (row by row) -> firmware input pin, computed from the board config
    matrix_bits = sorted(rows + cols)
    input_pin = lambda k: matrix_bits[k] if k < len(matrix_bits) else 30 + k - len(matrix_bits)
    net_of = {node: net for net, nodes in pcb.items() for node in nodes}
    seen = {}

    def slot_switch(a_net, b_net):
        """(kind, value) for the switch a pair of nets forms, checking its diode."""
        if generate.DIRECT_GPIO is not None and (a_net, b_net) == (f"GP{generate.DIRECT_GPIO}", "GND"):
            return "gp19"
        m = re.match(r"SW_R(\d+)C(\d+)$", a_net or "")
        if not m:
            return None
        r, c = int(m.group(1)) - 1, int(m.group(2)) - 1
        if b_net != f"COL{c + 1}":
            return None
        nodes = pcb[a_net]
        diodes = [ref for ref, pin in nodes if ref.startswith("D") and pin == "2"]
        if len(diodes) != 1 or net_of.get((diodes[0], "1")) != f"ROW{r + 1}":
            fail(f"{a_net}: needs exactly one diode, cathode on ROW{r + 1}")
        return r * len(cols) + c

    # connector pairs: pin N on A and B form one switch
    for p in range(generate.NUM_PAIRS):
        ra, rb = generate.pair_refs(p)
        for pin in range(1, generate.PAIR_PINS + 1):
            a_net, b_net = net_of.get((ra, str(pin))), net_of.get((rb, str(pin)))
            if a_net is None and b_net is None:
                continue
            sw = slot_switch(a_net, b_net)
            if sw is None:
                fail(f"{ra}/{rb} pin {pin}: {a_net} / {b_net} is not a switch")
                continue
            if sw in seen:
                fail(f"switch {sw} appears twice on the connectors")
            seen[sw] = (p, pin)
            # pair 1 follows the breakout header order
            if p == 0 and generate.HEADER[pin].startswith("GP"):
                g = int(generate.HEADER[pin][2:])
                want = "gp19" if g == generate.DIRECT_GPIO else None
                got = sw if sw == "gp19" else input_pin(sw)
                if (want or g) != got:
                    fail(f"pair 1 pin {pin} carries input pin {got}, breakout order needs GPIO {g}")
    want_direct = generate.DIRECT_GPIO is not None
    if ("gp19" in seen) != want_direct:
        fail(f"GPIO {generate.DIRECT_GPIO} direct slot missing or unexpected")
    order = sorted(seen, key=lambda s: seen[s])

    # screw terminals: physical order around the board must be the same slot
    # order, A then B for each slot
    board = pcbnew.LoadBoard(PCB)
    term_order = []
    for ref, edge, _ in generate.BLOCKS:
        fp = board.FindFootprintByReference(ref)
        pads = list(fp.Pads())
        key = (lambda q: q.GetPosition().x) if edge in ("top", "bottom") else (lambda q: q.GetPosition().y)
        pads.sort(key=key)
        nets = [q.GetNetname() for q in pads]
        for j in range(0, len(nets), 2):
            sw = slot_switch(nets[j], nets[j + 1])
            if sw is None:
                fail(f"{ref} terminals {j + 1}-{j + 2}: {nets[j]} / {nets[j + 1]} is not a switch")
            term_order.append(sw)
    # read around the board, the terminals carry the connector slots in the
    # same order; the terminal-only switches sit in between, in their own order.
    # Every switch once.
    switches = [t for t in term_order if t != "gp19"]
    extra = [t for t in term_order if t not in seen]
    if [t for t in term_order if t in seen] != order or extra != sorted(extra):
        fail("screw terminal order differs from the connector order")
    elif sorted(switches) != list(range(len(rows) * len(cols))):
        fail("screw terminals do not carry every switch exactly once")
    else:
        print(f"{len(order)} switches on connector pairs, {len(term_order) - len(order)} on screw terminals only")
        print(f"Matrix: {len(rows)} rows x {len(cols)} columns, {len(rows) * len(cols)} switches, one diode each; "
              f"connector pairs and screw terminals in the same order; pair 1 in breakout pin order")


def fab():
    os.makedirs(FAB, exist_ok=True)
    run = lambda *a: subprocess.run(["kicad-cli", *a], check=True, stdout=subprocess.DEVNULL)
    gerb = os.path.join(FAB, "gerbers")
    os.makedirs(gerb, exist_ok=True)
    run("pcb", "export", "gerbers", "--output", gerb + "/",
        "--layers", "F.Cu,B.Cu,F.Paste,B.Paste,F.SilkS,B.SilkS,F.Mask,B.Mask,Edge.Cuts", PCB)
    run("pcb", "export", "drill", "--output", gerb + "/", PCB)
    run("pcb", "export", "pos", "--output", os.path.join(FAB, NAME + "-pos.csv"),
        "--format", "csv", "--units", "mm", "--side", "both", PCB)
    run("sch", "export", "pdf", "--output", os.path.join(FAB, NAME + "-schematic.pdf"), SCH)
    run("pcb", "export", "svg", "--output", os.path.join(BUILD, NAME + "-front.svg"),
        "--layers", "F.Cu,F.SilkS,F.Mask,Edge.Cuts", "--page-size-mode", "2", "--exclude-drawing-sheet", PCB)
    run("pcb", "export", "svg", "--output", os.path.join(BUILD, NAME + "-back.svg"),
        "--layers", "B.Cu,B.SilkS,Edge.Cuts", "--page-size-mode", "2", "--exclude-drawing-sheet", "--mirror", PCB)

    # BOM grouped by part
    parts = {
        "Device:D": ("1N4148W", "Small-signal switching diode, SOD-123"),
        "Connector_Generic:Conn_02x10_Odd_Even": ("2x10 box header", "2.54 mm shrouded IDC box header, vertical, through-hole"),
        "Connector:Screw_Terminal_01x10": ("10-way terminal", "3.5 mm pitch screw terminal, Phoenix PT 1,5/10-3,5-H or equivalent"),
        "Connector:Screw_Terminal_01x12": ("12-way terminal", "3.5 mm pitch screw terminal, Phoenix PT 1,5/12-3,5-H or equivalent"),
    }
    groups = {}
    for p in generate.build_parts():
        if p["sym"] in parts:
            groups.setdefault((p["sym"], p["fp"]), []).append(p["ref"])
    with open(os.path.join(FAB, NAME + "-bom.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Qty", "References", "Value", "Footprint", "Description"])
        for (sym, fp), refs in groups.items():
            refs.sort(key=lambda r: int(re.sub(r"\D", "", r)))
            w.writerow([len(refs), " ".join(refs), parts[sym][0], fp, parts[sym][1]])
    print("Fab files written to fab/")


def main():
    check_breakout_header()
    drc()
    pcb = compare_netlists()
    check_firmware(pcb)
    fab()
    if errors:
        sys.exit(f"{len(errors)} check(s) failed")
    print("All checks passed")


if __name__ == "__main__":
    main()
