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


# Screw terminal blocks stand in rows, two behind each other above the headers
# (wire entry toward the top edge, pair 1) and two below them (wire entry
# toward the bottom edge, pair 2). The front row (at the board edge) holds pins
# 1-11 of its pair, the row behind it pins 12-20 and then two of the
# terminal-only switches. Each row is a 12-way and a 10-way block, read left to
# right.
ROW_SLOTS = 11


def _row_plan():
    p1 = list(range(PAIR_PINS))
    p2 = list(range(PAIR_PINS, 2 * PAIR_PINS))
    extra = list(range(2 * PAIR_PINS, len(SLOTS)))
    half = len(extra) // 2
    rows = [p1[:ROW_SLOTS], p1[ROW_SLOTS:] + extra[:half],
            p2[:ROW_SLOTS], p2[ROW_SLOTS:] + extra[half:]]
    return [(r, slots[:6]) for r, slots in enumerate(rows)] + \
           [(r, slots[6:]) for r, slots in enumerate(rows)]


# terminal blocks: (ref, edge the wire entry faces, slot indices), in reading
# order; BLOCK_ROW gives each block's row (0 = front)
_plan = sorted(_row_plan(), key=lambda b: (b[0], b[1][0]))
ROW_EDGE = ["top", "top", "bottom", "bottom"]   # edge each row's wire entry faces
ROW_DEPTH = [0, 1, 0, 1]                       # 0 = front row at that edge
BLOCKS = [(f"J{2 + 2 * NUM_PAIRS + i}", ROW_EDGE[r], slots) for i, (r, slots) in enumerate(_plan)]
BLOCK_ROW = [r for r, _ in _plan]
assert sorted(i for _, _, sl in BLOCKS for i in sl) == list(range(len(SLOTS)))


def slots_text(slots):
    """'Slots 13-17' or 'Slots 18-20, 41-42' (1-based slot numbers)."""
    runs, start = [], slots[0]
    for a, b in zip(slots, slots[1:] + [None]):
        if b != a + 1:
            runs.append(f"{start + 1}-{a + 1}" if a != start else f"{a + 1}")
            start = b
    return "Slots " + ", ".join(runs)


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
    for ref, edge, slots in BLOCKS:
        # pins in slot order: slot A, slot B, next slot A, ...
        pins = {}
        for j, s in enumerate(slots):
            a, b = slot_nets(SLOTS[s][2])
            pins[str(2 * j + 1)], pins[str(2 * j + 2)] = a, b
        n = 2 * len(slots)
        # blocks on the top and right edges are rotated so their pad 1 is at the
        # far end: number the pads so the slots still read left to right / top down
        if edge in ("top", "right"):
            pins = {str(n + 1 - int(k)): v for k, v in pins.items()}
        parts.append(dict(ref=ref, sym=f"Connector:Screw_Terminal_01x{n:02d}",
                          value=slots_text(slots), fp=fp_term(n), pins=pins, block=slots))
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
    for i, (ref, _, _) in enumerate(BLOCKS):
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
    out.append(f'  (title_block\n    (title "GP2040-CE Key Matrix Diode Adapter")\n    (rev "1.4")\n'
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

TERM_IN = 5.6         # front row pads, from the top edge
TERM_PITCH = 3.5
TERM_GAP = 7.0        # last pad of one block to first pad of the next in a row (leaves a routing channel)
TERM_FRONT = 5.0      # block body in front of its pads (wire entry side)
TERM_BACK = 3.6       # block body behind its pads
ROW_GAP = 6.0         # between one row's back and the next row's wire entry: labels, diodes, wire room
LABEL_BACK = 1.0      # slot label, behind the block body
DIODE_BACK = 3.6      # diode centre, behind the block body
HEADER_PITCH = 14.0
SIDE_MARGIN = 2.75    # board edge to the end of a terminal row
NUM_ROWS = max(BLOCK_ROW) + 1


def _row_len():
    n = [2 * len(sl) for (_, _, sl), r in zip(BLOCKS, BLOCK_ROW) if r == 0]
    return sum(k - 1 for k in n) * TERM_PITCH + (len(n) - 1) * TERM_GAP


ROW_PITCH = TERM_FRONT + TERM_BACK + ROW_GAP
BOARD_W = 91.0
TB_START = (BOARD_W - _row_len()) / 2          # first pad of each row
assert TB_START - 2.25 >= SIDE_MARGIN
DIODE_REACH = DIODE_BACK + 1.15                # block back to the far side of its diodes
INNER_BACK = TERM_IN + ROW_PITCH + TERM_BACK   # back of the inner rows, from their edge
HEADER_Y = round(INNER_BACK + DIODE_REACH + 0.3 + 5.6, 1)    # header pin 1, below the top rows' diodes
HEADER_LABEL_Y = HEADER_Y + 28.46 + 1.1
BOARD_H = round(HEADER_LABEL_Y + 0.8 + DIODE_REACH + INNER_BACK, 1)


def row_y(r):
    d = TERM_IN + ROW_DEPTH[r] * ROW_PITCH
    return d if ROW_EDGE[r] == "top" else BOARD_H - d


def block_layout(i):
    """Edge the wire entry faces and first pad x of terminal block i."""
    r = BLOCK_ROW[i]
    pos = TB_START
    for j in range(i):
        if BLOCK_ROW[j] == r:
            pos += (2 * len(BLOCKS[j][2]) - 1) * TERM_PITCH + TERM_GAP
    return ROW_EDGE[r], pos


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
    nc.SetTrackWidth(mm(0.2))
    nc.SetClearance(mm(0.2))
    nc.SetViaDiameter(mm(0.6))
    nc.SetViaDrill(mm(0.3))

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

    # Terminal blocks: rows one behind another, wire entry toward their edge.
    # Slots run left to right, A then B for each slot. Behind each row are its
    # slot labels and then its diodes, one behind each A terminal.
    slot_label_pos = []
    diode_of = {f"SW_R{p['row'] + 1}C{p['col'] + 1}": p for p in parts if p["ref"].startswith("D")}
    for i, (ref, _, slots) in enumerate(BLOCKS):
        edge, first = block_layout(i)
        n = 2 * len(slots)
        last = first + (n - 1) * TERM_PITCH
        y = row_y(BLOCK_ROW[i])
        inward = 1 if edge == "top" else -1
        back = y + inward * TERM_BACK
        # top rows are rotated 180 so the wire entry faces the top edge, which
        # puts pad n leftmost; build_parts numbered the pads to match
        if edge == "top":
            fps[ref] = add(byref[ref], last, y, 180)
        else:
            fps[ref] = add(byref[ref], first, y, 0)
        for j, s in enumerate(slots):
            ax = first + 2 * j * TERM_PITCH
            pair, pin, item = SLOTS[s]
            slot_label_pos.append((slot_label(pair, pin, item), ax + TERM_PITCH / 2, back + inward * LABEL_BACK))
            if item[0] == "sw":
                d = diode_of[slot_nets(item)[0]]
                fps[d["ref"]] = add(d, ax, back + inward * DIODE_BACK, 0)

    # Headers stand vertically in a row between the terminal rows, pin 1 at the top
    # left: J1 to the breakout, then the switch connector pairs (A, B, A, B, ...).
    header_refs = ["J1"] + [r for p in range(NUM_PAIRS) for r in pair_refs(p)]
    span = (len(header_refs) - 1) * HEADER_PITCH + 2.54
    hx0 = (BOARD_W - span) / 2
    hy0 = HEADER_Y
    for i, ref in enumerate(header_refs):
        fps[ref] = add(byref[ref], hx0 + i * HEADER_PITCH, hy0, 0)

    # mounting holes beside the headers
    for i, (x, y) in enumerate([(3.8, hy0 + 1.0), (3.8, HEADER_LABEL_Y - 3.0),
                                (BOARD_W - 3.8, hy0 + 1.0), (BOARD_W - 3.8, HEADER_LABEL_Y - 3.0)]):
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

    ly2 = HEADER_LABEL_Y
    silk("BREAKOUT", hx0 + 1.27, ly2, 1.0, bold=True)
    for p in range(NUM_PAIRS):
        for i, side in enumerate("AB"):
            x = hx0 + (1 + 2 * p + i) * HEADER_PITCH + 1.27
            silk(f"PAIR {p + 1} {side}", x, ly2, 1.0, bold=True)

    path = os.path.join(HERE, NAME + ".kicad_pcb")
    board.SetFileName(path)
    pcbnew.SaveBoard(path, board)
    return path


def route():
    import pcbnew
    import autoroute

    path = os.path.join(HERE, NAME + ".kicad_pcb")
    first = []  # nets that failed before get routed first next time
    for attempt in range(12):
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
