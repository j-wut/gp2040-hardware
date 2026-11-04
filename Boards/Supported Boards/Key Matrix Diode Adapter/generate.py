#!/usr/bin/env python3
"""Generate the GP2040-CE key matrix diode adapter (KiCad 7 schematic + PCB).

The adapter plugs into the 20-pin header of the RP2040 Advanced Breakout Board
(USB Passthrough edition) with a straight 20-pin IDC ribbon cable. It turns the
15 button GPIOs on that header into a 4 x 11 diode matrix with one screw terminal
and one diode per switch, matching the RP2040AdvancedBreakoutBoardUSBPassthroughMatrix
board config.

Usage (needs KiCad 7 with its Python module and libraries installed):
    python3 generate.py     # write schematic and routed PCB (autoroute.py)
    python3 check.py        # DRC, schematic vs PCB netlist, manufacturing files

Everything (parts, nets, placement) comes from the tables below, so the
schematic and the PCB cannot drift apart. check.py compares them afterwards.
"""

import argparse
import os
import re
import subprocess
import sys
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
NAME = "keymatrix-adapter"
KICAD_SYM = "/usr/share/kicad/symbols"
KICAD_FP = "/usr/share/kicad/footprints"

# ---------------------------------------------------------------------------
# Electrical design
# ---------------------------------------------------------------------------

COL_GPIOS = [5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]  # must match KEY_MATRIX_COL_PINS
ROW_GPIOS = [16, 17, 18, 19]                          # must match KEY_MATRIX_ROW_PINS
DIRECT_GPIO = None  # a header GPIO left as a normal button wired to GND, if any

# Breakout board 20-pin header, pin number -> signal, from the footprint pad
# numbers in the board's source file (Boards/GP2040-CE Official Boards/RP2040
# Advanced Breakout Board/RP2040 Advanced Breakout Board - Passthrough/Source
# files, v5.6): pins 1/2 are +5 V, pin 20 is UP.
HEADER = {
    1: "+5V", 2: "+5V", 3: "GND", 4: "GND", 5: "GP5", 6: "GP6",
    7: "GP7", 8: "GP8", 9: "GND", 10: "GP9", 11: "GP10", 12: "GP11",
    13: "GP12", 14: "GP13", 15: "GP14", 16: "GP15", 17: "GP16", 18: "GP17",
    19: "GP18", 20: "GP19",
}

# Silkscreen names the breakout board prints for each GPIO terminal.
BREAKOUT_LABEL = {5: "K4", 6: "K3", 7: "K2", 8: "K1", 9: "P4", 10: "P3", 11: "P2",
                  12: "P1", 13: "S2", 14: "A1", 15: "S1", 16: "L", 17: "R", 18: "D", 19: "U"}


def col_net(c):
    return f"COL{c + 1}"


def row_net(r):
    return f"ROW{r + 1}"


def sw_net(r, c):
    return f"SW_R{r + 1}C{c + 1}"


def gpio_net(gpio):
    if gpio in COL_GPIOS:
        return col_net(COL_GPIOS.index(gpio))
    if gpio in ROW_GPIOS:
        return row_net(ROW_GPIOS.index(gpio))
    return f"GP{gpio}"


# Switch connector pairs (A and B): pin N on A and pin N on B are the two sides
# of one switch. Every pair has the same order. Pair 1 follows the breakout's
# 20-pin header: each of its 15 button pins carries the switch that defaults to
# that pin's button (a direct button, if any, has GND on B). Its 5
# power/ground positions and pairs 2-3 carry the other switches in row order.
# The screw terminals repeat the same slots in the same order, A then B.
PAIR_PINS = 20
NUM_PAIRS = 2  # switches beyond the pairs are on screw terminals only
NUM_SWITCHES = len(ROW_GPIOS) * len(COL_GPIOS)


def switch_rc(k):
    """Switch number k (0-based, row by row) -> (row, col)."""
    return divmod(k, len(COL_GPIOS))


def switch_input_pin(k):
    """Firmware input pin of switch k: the matrix GPIO bits first, then 30 and up
    (same allocation as KeyMatrix::setup)."""
    matrix_bits = sorted(COL_GPIOS + ROW_GPIOS)
    return matrix_bits[k] if k < len(matrix_bits) else 30 + k - len(matrix_bits)


def build_slots():
    """Ordered list of (pair, pin, item); item is ("sw", k) or ("gp19",)."""
    slots = {}
    by_input = {switch_input_pin(k): k for k in range(NUM_SWITCHES)}
    used = set()
    for pin in range(1, PAIR_PINS + 1):
        sig = HEADER[pin]
        if not sig.startswith("GP"):
            continue
        g = int(sig[2:])
        if DIRECT_GPIO is not None and g == DIRECT_GPIO:
            slots[(0, pin)] = ("gp19",)
        elif g in by_input:
            slots[(0, pin)] = ("sw", by_input[g])
            used.add(by_input[g])
    free = [(0, pin) for pin in range(1, PAIR_PINS + 1) if (0, pin) not in slots]
    free += [(p, pin) for p in range(1, NUM_PAIRS) for pin in range(1, PAIR_PINS + 1)]
    # terminal-only slots, numbered as if they were the next pair
    free += [(NUM_PAIRS, n) for n in range(1, NUM_SWITCHES + 1)]
    rest = [k for k in range(NUM_SWITCHES) if k not in used]
    if len(rest) > len(free):
        raise RuntimeError("not enough connector positions")
    for slot, k in zip(free, rest):
        slots[slot] = ("sw", k)
    return [(p, pin, slots[(p, pin)]) for p, pin in sorted(slots)]


SLOTS = build_slots()


def slot_nets(item):
    if item[0] == "gp19":
        return f"GP{DIRECT_GPIO}", "GND"
    r, c = switch_rc(item[1])
    return sw_net(r, c), col_net(c)   # A: through the switch's diode to its row; B: column


def slot_label(pair, pin, item):
    if item[0] == "gp19":
        return BREAKOUT_LABEL[DIRECT_GPIO]
    g = switch_input_pin(item[1])
    if pair == 0 and HEADER[pin] == f"GP{g}":
        return BREAKOUT_LABEL[g]
    r, c = switch_rc(item[1])
    return f"R{r + 1}C{c + 1}"


def pair_refs(p):
    return f"J{2 + 2 * p}", f"J{3 + 2 * p}"


SLOTS_PER_BLOCK = 5
# terminal blocks: (ref, first slot index, slot count)
BLOCKS = []
for _i, _start in enumerate(range(0, len(SLOTS), SLOTS_PER_BLOCK)):
    BLOCKS.append((f"J{2 + 2 * NUM_PAIRS + _i}", _start, min(SLOTS_PER_BLOCK, len(SLOTS) - _start)))


def header_net(pin):
    sig = HEADER[pin]
    return gpio_net(int(sig[2:])) if sig.startswith("GP") else sig


FP_HEADER = "Connector_IDC:IDC-Header_2x10_P2.54mm_Vertical"


def fp_term(n):
    return f"TerminalBlock_Phoenix:TerminalBlock_Phoenix_PT-1,5-{n}-3.5-H_1x{n:02d}_P3.50mm_Horizontal"


FP_DIODE = "Diode_SMD:D_SOD-123"
FP_HOLE = "MountingHole:MountingHole_3.2mm_M3"


def build_parts():
    """List of parts: ref, lib symbol, value, footprint, {pin: net}."""
    parts = []
    hdr = {str(p): header_net(p) for p in HEADER}
    parts.append(dict(ref="J1", sym="Connector_Generic:Conn_02x10_Odd_Even",
                      value="To breakout", fp=FP_HEADER, pins=hdr))
    for p in range(NUM_PAIRS):
        a, b = {}, {}
        for pair, pin, item in SLOTS:
            if pair == p:
                a[str(pin)], b[str(pin)] = slot_nets(item)
        ra, rb = pair_refs(p)
        parts.append(dict(ref=ra, sym="Connector_Generic:Conn_02x10_Odd_Even",
                          value=f"Pair {p + 1} A", fp=FP_HEADER, pins=a))
        parts.append(dict(ref=rb, sym="Connector_Generic:Conn_02x10_Odd_Even",
                          value=f"Pair {p + 1} B", fp=FP_HEADER, pins=b))
    for ref, start, count in BLOCKS:
        # pins in slot order: slot A, slot B, next slot A, ...
        pins = {}
        for j in range(count):
            a, b = slot_nets(SLOTS[start + j][2])
            pins[str(2 * j + 1)], pins[str(2 * j + 2)] = a, b
        n = 2 * count
        # blocks on the top and right edges are rotated so their pad 1 is at the
        # far end: number the pads so the slots still read left to right / top down
        if block_layout(len([q for q in parts if q.get("block")]))[0] in ("top", "right"):
            pins = {str(n + 1 - int(k)): v for k, v in pins.items()}
        parts.append(dict(ref=ref, sym=f"Connector:Screw_Terminal_01x{n:02d}",
                          value=f"Slots {start + 1}-{start + count}", fp=fp_term(n), pins=pins,
                          block=(start, count)))
    for k in range(NUM_SWITCHES):
        r, c = switch_rc(k)
        # pin 1 = K (cathode, stripe) to the row, pin 2 = A to the switch
        parts.append(dict(ref=f"D{k + 1}", sym="Device:D", value="1N4148W", fp=FP_DIODE,
                          pins={"1": row_net(r), "2": sw_net(r, c)}, row=r, col=c, k=k))
    for i in range(4):
        parts.append(dict(ref=f"H{i + 1}", sym="Mechanical:MountingHole", value="M3",
                          fp=FP_HOLE, pins={}))
    return parts


# ---------------------------------------------------------------------------
# Schematic
# ---------------------------------------------------------------------------

def uid():
    return str(uuid.uuid4())


def extract_symbol(lib, name):
    text = open(os.path.join(KICAD_SYM, lib + ".kicad_sym")).read()
    start = text.index(f'\n  (symbol "{name}"') + 1
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                body = text[start:i + 1]
                break
    if "(extends" in body:
        raise RuntimeError(f"{lib}:{name} extends another symbol")
    pins = {}
    for m in re.finditer(r'\(pin \w+ \w+ \(at ([-\d.]+) ([-\d.]+) ([-\d.]+)\).*?\(number "([^"]+)"', body, re.S):
        pins[m.group(4)] = (float(m.group(1)), float(m.group(2)), float(m.group(3)))
    body = body.replace(f'(symbol "{name}"', f'(symbol "{lib}:{name}"', 1)
    return body, pins


def sch_pos(x, y, px, py):
    # symbol library is y-up, schematic is y-down (all symbols placed unrotated)
    return round(x + px, 2), round(y - py, 2)


def snap(v):
    return round(round(v / 1.27) * 1.27, 2)


def build_schematic(parts):
    root = uid()
    libs = {}
    for p in parts:
        lib, name = p["sym"].split(":")
        if p["sym"] not in libs:
            libs[p["sym"]] = extract_symbol(lib, name)

    out = []
    items = []

    def text(s, x, y, size=1.27):
        items.append(f'  (text "{s}" (at {x} {y} 0)\n    (effects (font (size {size} {size})) (justify left bottom))\n    (uuid {uid()})\n  )')

    def wire(x1, y1, x2, y2):
        items.append(f'  (wire (pts (xy {x1} {y1}) (xy {x2} {y2}))\n    (stroke (width 0) (type default))\n    (uuid {uid()})\n  )')

    def label(net, x, y, angle):
        just = "right bottom" if angle == 180 else "left bottom"
        items.append(f'  (label "{net}" (at {x} {y} {angle}) (fields_autoplaced)\n    (effects (font (size 1.27 1.27)) (justify {just}))\n    (uuid {uid()})\n  )')

    def power(net, x, y, angle):
        items.append(f'  (global_label "{net}" (shape passive) (at {x} {y} {angle}) (fields_autoplaced)\n    (effects (font (size 1.27 1.27)) (justify {"right" if angle == 180 else "left"}))\n    (uuid {uid()})\n  )')

    def place(p, x, y, ref_at=None, val_at=None):
        x, y = snap(x), snap(y)
        _, pins = libs[p["sym"]]
        rx, ry = ref_at or (x, y - 2.54)
        vx, vy = val_at or (x, y + 2.54)
        pin_uuids = "".join(f'\n    (pin "{n}" (uuid {uid()}))' for n in pins)
        items.append(
            f'  (symbol (lib_id "{p["sym"]}") (at {x} {y} 0) (unit 1)\n'
            f'    (in_bom yes) (on_board yes) (dnp no)\n'
            f'    (uuid {uid()})\n'
            f'    (property "Reference" "{p["ref"]}" (at {rx} {ry} 0)\n      (effects (font (size 1.27 1.27)))\n    )\n'
            f'    (property "Value" "{p["value"]}" (at {vx} {vy} 0)\n      (effects (font (size 1.27 1.27)))\n    )\n'
            f'    (property "Footprint" "{p["fp"]}" (at {x} {y} 0)\n      (effects (font (size 1.27 1.27)) hide)\n    )\n'
            f'    (property "Datasheet" "~" (at {x} {y} 0)\n      (effects (font (size 1.27 1.27)) hide)\n    )'
            f'{pin_uuids}\n'
            f'    (instances\n      (project "{NAME}"\n        (path "/{root}"\n          (reference "{p["ref"]}") (unit 1)\n        )\n      )\n    )\n  )')
        # wire stub + net label on every connected pin
        for n, net in p["pins"].items():
            px, py, pa = pins[n]
            ex, ey = sch_pos(x, y, px, py)
            # pin points away from the body: angle 0 = pin on the left side
            dx = -2.54 if pa == 0 else 2.54
            if pa in (90, 270):
                dx = 0
            lx = round(ex + dx, 2)
            wire(ex, ey, lx, ey)
            if net in ("GND", "+5V"):
                power(net, lx, ey, 180 if dx < 0 else 0)
            else:
                label(net, lx, ey, 180 if dx < 0 else 0)

    byref = {p["ref"]: p for p in parts}
    # connectors on the left
    text("Breakout board 20-pin header (straight IDC ribbon, pin 1 to pin 1)", 20.32, 25.4)
    hdr_txt = lambda y: dict(ref_at=(39.37, snap(y) - 15.24), val_at=(39.37, snap(y) + 15.24))
    place(byref["J1"], 38.1, 45.72, **hdr_txt(45.72))
    text("Switch connector pairs: pin N on A and pin N on B are the two sides of one switch, same order on every pair.", 20.32, 205.74)
    text("Pair 1 follows the breakout header: each button pin carries the switch that defaults to that button.", 20.32, 209.55)
    for p in range(NUM_PAIRS):
        for i, ref in enumerate(pair_refs(p)):
            x = 38.1 + (2 * p + i) * 63.5
            place(byref[ref], x, 233.68, **dict(ref_at=(snap(x) + 1.27, 218.44), val_at=(snap(x) + 1.27, 248.92)))
    for i in range(4):
        place(byref[f"H{i + 1}"], 25.4 + i * 12.7, 165.1)

    text("Screw terminals: the same slots in the same order. Each switch uses two neighbouring terminals (A, B).", 88.9, 22.86)
    text("A: through the switch's diode to its row. B: its column (GP19 slot: GPIO 19 and GND).", 88.9, 26.67)
    for i, (ref, start, count) in enumerate(BLOCKS):
        x = 101.6 + (i % 5) * 63.5
        y = 50.8 + (i // 5) * 50.8
        place(byref[ref], x, y, ref_at=(snap(x), snap(y) - 15.24), val_at=(snap(x), snap(y) + 17.78))
    text(f"Diodes: one per switch, cathode (stripe) to the row. D1-D{NUM_SWITCHES} = switch R1C1 ... R{len(ROW_GPIOS)}C{len(COL_GPIOS)}, 1N4148W SOD-123.", 88.9, 140.97)
    for p in parts:
        if p["ref"].startswith("D"):
            r, c = p["row"], p["col"]
            x, y = 101.6 + r * 63.5, 147.32 + c * 5.08
            place(p, x, y, ref_at=(snap(x), snap(y) - 1.9), val_at=(snap(x) + 12, snap(y) + 30))

    out.append('(kicad_sch (version 20230121) (generator eeschema)\n')
    out.append(f'  (uuid {root})\n')
    out.append('  (paper "A3")\n')
    out.append(f'  (title_block\n    (title "GP2040-CE Key Matrix Diode Adapter")\n    (rev "1.2")\n'
               f'    (comment 1 "For RP2040 Advanced Breakout Board - USB Passthrough, 20-pin header")\n'
               f'    (comment 2 "Firmware: RP2040AdvancedBreakoutBoardUSBPassthroughMatrix")\n  )\n')
    out.append('  (lib_symbols\n')
    for body, _ in libs.values():
        out.append("    " + body.replace("\n", "\n  ") + "\n")
    out.append('  )\n')
    out.append("\n".join(items) + "\n")
    out.append(f'  (sheet_instances\n    (path "/" (page "1"))\n  )\n)\n')
    # value text of diodes would overlap; hide them
    sch = "".join(out)
    sch = re.sub(r'(\(property "Value" "1N4148W" \(at [^)]*\)\n      \(effects \(font \(size 1.27 1.27\)\))\)',
                 r'\1 hide)', sch)
    with open(os.path.join(HERE, NAME + ".kicad_sch"), "w") as f:
        f.write(sch)


# ---------------------------------------------------------------------------
# PCB
# ---------------------------------------------------------------------------

BOARD_W = 156.0
BOARD_H = 112.0
TERM_IN = 5.6         # terminal pads, measured in from the board edge
TERM_PITCH = 3.5
BLOCK_PITCH = 37.0    # 10-way blocks, four along the top and four along the bottom
HEADER_PITCH = 17.0
DIODE_IN = 9.0        # diode centre, measured inward from its terminal pad
LABEL_IN = 5.0        # slot label, measured inward from the terminal pads


def block_layout(i):
    """Edge and position of terminal block i: pair 1 slots along the top edge,
    pair 2 along the bottom (both left to right), pair 3 down the left then
    the right edge. Returns (edge, first pad x or y in visual order)."""
    if i < 8:
        row_w = 3 * BLOCK_PITCH + 9 * TERM_PITCH
        x0 = (BOARD_W - row_w) / 2
        return ("top" if i < 4 else "bottom"), x0 + (i % 4) * BLOCK_PITCH
    n = 2 * BLOCKS[i][2]
    return ("left" if i == 8 else "right"), BOARD_H / 2 - (n - 1) * TERM_PITCH / 2


def build_pcb(parts):
    import pcbnew

    board = pcbnew.BOARD()
    mm = pcbnew.FromMM

    def vec(x, y):
        return pcbnew.VECTOR2I(mm(x), mm(y))

    nets = {}

    def net(name):
        if name not in nets:
            ni = pcbnew.NETINFO_ITEM(board, name)
            board.Add(ni)
            nets[name] = ni
        return nets[name]

    # outline
    pts = [(0, 0), (BOARD_W, 0), (BOARD_W, BOARD_H), (0, BOARD_H)]
    for i in range(4):
        seg = pcbnew.PCB_SHAPE(board)
        seg.SetShape(pcbnew.SHAPE_T_SEGMENT)
        seg.SetStart(vec(*pts[i]))
        seg.SetEnd(vec(*pts[(i + 1) % 4]))
        seg.SetLayer(pcbnew.Edge_Cuts)
        seg.SetWidth(mm(0.1))
        board.Add(seg)

    ds = board.GetDesignSettings()
    ds.m_TrackMinWidth = mm(0.2)
    ds.m_MinClearance = mm(0.2)
    ds.m_ViasMinSize = mm(0.6)
    ds.m_MinThroughDrill = mm(0.3)
    ds.m_CopperEdgeClearance = mm(0.3)
    nc = ds.m_NetSettings.m_DefaultNetClass
    nc.SetTrackWidth(mm(0.3))
    nc.SetClearance(mm(0.2))
    nc.SetViaDiameter(mm(0.7))
    nc.SetViaDrill(mm(0.35))

    def add(p, x, y, angle):
        lib, name = p["fp"].split(":")
        fp = pcbnew.FootprintLoad(os.path.join(KICAD_FP, lib + ".pretty"), name)
        fp.SetReference(p["ref"])
        fp.SetValue(p["value"])
        fp.SetFPIDAsString(p["fp"])
        board.Add(fp)
        fp.SetOrientationDegrees(angle)
        fp.SetPosition(vec(x, y))
        for pad in fp.Pads():
            n = pad.GetNumber()
            if n in p["pins"]:
                pad.SetNet(net(p["pins"][n]))
        return fp

    byref = {p["ref"]: p for p in parts}
    fps = {}

    # Terminal blocks: wire entry faces the board edge. Slots run left to right
    # (top and bottom edges) or top to bottom (sides), A then B for each slot.
    # Each slot's diode sits right behind its A terminal, anode toward it.
    slot_label_pos = []
    diode_of = {f"SW_R{p['row'] + 1}C{p['col'] + 1}": p for p in parts if p["ref"].startswith("D")}
    for i, (ref, start, count) in enumerate(BLOCKS):
        edge, first = block_layout(i)
        n = 2 * count
        last = first + (n - 1) * TERM_PITCH
        if edge == "top":       # rotated 180: pad 1 on the right... so pad n is leftmost
            pos = lambda v: (v, TERM_IN)
            inward = (0, 1)
            anchor, angle = pos(last), 180
        elif edge == "bottom":
            pos = lambda v: (v, BOARD_H - TERM_IN)
            inward = (0, -1)
            anchor, angle = pos(first), 0
        elif edge == "left":    # rotated 270: pad 1 at the top
            pos = lambda v: (TERM_IN, v)
            inward = (1, 0)
            anchor, angle = pos(first), 270
        else:                   # rotated 90: pad 1 at the bottom
            pos = lambda v: (BOARD_W - TERM_IN, v)
            inward = (-1, 0)
            anchor, angle = pos(last), 90
        # build_parts numbered the pads in slot order (1 = first A). Re-number
        # them to the footprint's physical order.
        fps[ref] = add(byref[ref], *anchor, angle)
        for j in range(count):
            ax, ay = pos(first + 2 * j * TERM_PITCH)
            cx, cy = pos(first + (2 * j + 0.5) * TERM_PITCH)
            pair, pin, item = SLOTS[start + j]
            # side labels are horizontal, so move them further in to clear the block
            lin = LABEL_IN if edge in ("top", "bottom") else LABEL_IN + 1.4
            slot_label_pos.append((slot_label(pair, pin, item), cx + inward[0] * lin,
                                   cy + inward[1] * lin))
            if item[0] == "sw":
                d = diode_of[slot_nets(item)[0]]
                dx, dy = ax + inward[0] * DIODE_IN, ay + inward[1] * DIODE_IN
                d_angle = {"top": 90, "bottom": 270, "left": 180, "right": 0}[edge]
                fps[d["ref"]] = add(d, dx, dy, d_angle)

    # Headers stand vertically in a row across the middle, pin 1 at the top
    # left: J1 to the breakout, then the switch connector pairs (A, B, A, B, ...).
    header_refs = ["J1"] + [r for p in range(NUM_PAIRS) for r in pair_refs(p)]
    span = (len(header_refs) - 1) * HEADER_PITCH + 2.54
    hx0 = (BOARD_W - span) / 2
    hy0 = BOARD_H / 2 - 9 * 2.54 / 2 - 2
    for i, ref in enumerate(header_refs):
        fps[ref] = add(byref[ref], hx0 + i * HEADER_PITCH, hy0, 0)

    for i, (x, y) in enumerate([(6.0, 26.0), (6.0, BOARD_H - 26.0),
                                (BOARD_W - 6.0, 26.0), (BOARD_W - 6.0, BOARD_H - 26.0)]):
        fps[f"H{i + 1}"] = add(byref[f"H{i + 1}"], x, y, 0)

    # Reference designators stay on the fab layer; the silkscreen gets plain
    # labels instead (column and row names, connector names).
    for fp in fps.values():
        fp.Reference().SetVisible(False)
        fp.Value().SetVisible(False)

    def silk(s, x, y, size=1.0, layer=None, angle=0, bold=False):
        t = pcbnew.PCB_TEXT(board)
        t.SetText(s)
        t.SetPosition(vec(x, y))
        t.SetLayer(layer if layer is not None else pcbnew.F_SilkS)
        t.SetTextSize(vec(size, size))
        t.SetTextThickness(mm(size * (0.2 if bold else 0.15)))
        t.SetTextAngleDegrees(angle)
        board.Add(t)

    for text_, x, y in slot_label_pos:
        silk(text_, x, y, 0.9)

    ly2 = hy0 + 9 * 2.54 + 7.0
    silk("BREAKOUT", hx0 + 1.27, ly2, 1.0, bold=True)
    for p in range(NUM_PAIRS):
        for i, side in enumerate("AB"):
            x = hx0 + (1 + 2 * p + i) * HEADER_PITCH + 1.27
            silk(f"PAIR {p + 1} {side}", x, ly2, 1.0, bold=True)
    silk("GP2040-CE KEY MATRIX ADAPTER v1.2", BOARD_W / 2, ly2 + 3.0, 1.2, bold=True)
    silk("SWITCH: PIN N ON A <-> PIN N ON B, OR ITS TWO SCREW TERMINALS. PAIR 1 = BREAKOUT PIN ORDER",
         BOARD_W / 2, ly2 + 5.0, 0.9)
    silk("TERMINALS: TOP = PAIR 1, BOTTOM = PAIR 2, LEFT SIDE = TERMINALS ONLY", BOARD_W / 2, ly2 + 6.8, 0.9)

    path = os.path.join(HERE, NAME + ".kicad_pcb")
    board.SetFileName(path)
    pcbnew.SaveBoard(path, board)
    return path


def route():
    import pcbnew
    import autoroute

    path = os.path.join(HERE, NAME + ".kicad_pcb")
    first = []  # nets that failed before get routed first next time
    for attempt in range(8):
        board = pcbnew.LoadBoard(path)
        r = autoroute.Router(board, BOARD_W, BOARD_H)
        # group pads by net
        by_net = {}
        for key, (code, cells, centre, layers) in r.pad_cells.items():
            by_net.setdefault(code, []).append((layers, cells, centre, key))
        netinfo = board.GetNetInfo()
        order = []
        for code in by_net:
            n = netinfo.GetNetItem(code).GetNetname()
            rank = 0 if n.startswith("SW_") else 1 if n.startswith("ROW") else 2 if n.startswith("COL") else 3
            if n in first:
                rank = -1 - (len(first) - first.index(n))
            order.append((rank, len(by_net[code]), n, code))
        order.sort()
        failed = []
        for rank, count, n, code in order:
            terms = sorted(by_net[code], key=lambda t: (t[2][0], t[2][1]))
            if not r.route_net(code, [(t[0], t[1], t[2]) for t in terms]):
                failed.append(n)
        if not failed:
            break
        print(f"attempt {attempt + 1}: could not route {', '.join(failed)}; retrying with them first")
        first = [n for n in first if n not in failed] + failed
    r.write()

    # ground pour on both layers
    gnd = board.FindNet("GND")
    for layer in (pcbnew.F_Cu, pcbnew.B_Cu):
        z = pcbnew.ZONE(board)
        z.SetLayer(layer)
        z.SetNet(gnd)
        z.SetLocalClearance(pcbnew.FromMM(0.3))
        z.SetMinThickness(pcbnew.FromMM(0.25))
        z.SetPadConnection(pcbnew.ZONE_CONNECTION_FULL)  # few GND pads; avoids starved thermals
        ol = z.Outline()
        ol.NewOutline()
        for x, y in [(0.5, 0.5), (BOARD_W - 0.5, 0.5), (BOARD_W - 0.5, BOARD_H - 0.5), (0.5, BOARD_H - 0.5)]:
            ol.Append(pcbnew.FromMM(x), pcbnew.FromMM(y))
        board.Add(z)
    pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    pcbnew.SaveBoard(path, board)
    if failed:
        sys.exit("could not route: " + ", ".join(failed))
    print(f"routed {len(order)} nets, {len(r.via_list)} vias")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-route", action="store_true", help="leave the PCB unrouted")
    args = ap.parse_args()
    parts = build_parts()
    build_schematic(parts)
    build_pcb(parts)
    if not args.no_route:
        route()


if __name__ == "__main__":
    main()
