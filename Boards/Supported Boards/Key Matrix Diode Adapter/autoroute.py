"""A small two-layer grid router for the key matrix adapter.

Tracks run on a 0.4 mm grid, horizontal and vertical only, so two tracks on
neighbouring grid lines keep 0.2 mm of clearance with 0.2 mm wide tracks.
Each net is grown as a tree: Dijkstra from everything already routed for the
net to the nearest unrouted pad. F.Cu prefers vertical runs and B.Cu prefers
horizontal runs, and vias cost extra, which gives a tidy layout. KiCad's own
DRC checks the result afterwards (see check.py).
"""

import heapq
import math

import pcbnew

GRID = 0.4
TRACK_W = 0.2
CLEAR = 0.2
VIA_D = 0.6
VIA_DRILL = 0.3
EDGE_CLEAR = 0.5
VIA_COST = 12
WRONG_WAY = 3
# grid steps between two allowed cells can pass closer to a round pad than
# either cell does; this keeps that within the clearance
MARGIN = 0.08  # cost per step against a layer's preferred direction

FREE = 0
BLOCKED = -1

# grid offsets around a via that are too close for another net's track
VIA_NEIGH = [(dx, dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1) if (dx, dy) != (0, 0)
             and math.hypot(dx, dy) * GRID < VIA_D / 2 + TRACK_W / 2 + CLEAR]


class Router:
    def __init__(self, board, width, height):
        self.board = board
        self.nx = int(round(width / GRID)) + 1
        self.ny = int(round(height / GRID)) + 1
        n = self.nx * self.ny
        # per layer: net code that owns the cell (track, pad copper or keep-out)
        self.occ = [[FREE] * n, [FREE] * n]
        # per layer: net code that may place a via here (BLOCKED = nobody)
        self.via_ok = [[FREE] * n, [FREE] * n]
        self.vias = set()
        self.segments = []  # (layer, x1, y1, x2, y2, net)
        self.via_list = []  # (x, y, net)
        self.pad_cells = {}  # (ref, padnum) -> list of (layer, idx)
        self.width, self.height = width, height
        self._mark_edges()
        self._mark_pads()

    def idx(self, gx, gy):
        return gy * self.nx + gx

    def xy(self, i):
        return (i % self.nx) * GRID, (i // self.nx) * GRID

    def _mark_edges(self):
        for gy in range(self.ny):
            for gx in range(self.nx):
                x, y = gx * GRID, gy * GRID
                d = min(x, y, self.width - x, self.height - y)
                i = self.idx(gx, gy)
                if d < EDGE_CLEAR + TRACK_W / 2:
                    for l in (0, 1):
                        self.occ[l][i] = BLOCKED
                if d < EDGE_CLEAR + VIA_D / 2:
                    for l in (0, 1):
                        self.via_ok[l][i] = BLOCKED

    @staticmethod
    def _dist(shape, x, y):
        kind, cx, cy, hw, hh = shape
        if kind == "circle":
            return math.hypot(x - cx, y - cy) - hw
        ox, oy = abs(x - cx) - hw, abs(y - cy) - hh
        if ox <= 0 and oy <= 0:
            return max(ox, oy)  # inside: negative depth
        return math.hypot(max(ox, 0), max(oy, 0))

    def _cells_near(self, shape, reach):
        _, cx, cy, hw, hh = shape
        r = max(hw, hh) + reach
        for gy in range(max(0, int((cy - r) / GRID)), min(self.ny, int((cy + r) / GRID) + 2)):
            for gx in range(max(0, int((cx - r) / GRID)), min(self.nx, int((cx + r) / GRID) + 2)):
                yield gx, gy, self._dist(shape, gx * GRID, gy * GRID)

    def _mark_pads(self):
        mm = pcbnew.ToMM
        for fp in self.board.GetFootprints():
            for pad in fp.Pads():
                pos = pad.GetPosition()
                cx, cy = mm(pos.x), mm(pos.y)
                bb = pad.GetBoundingBox()
                hw, hh = mm(bb.GetWidth()) / 2, mm(bb.GetHeight()) / 2
                circle = pad.GetShape() == pcbnew.PAD_SHAPE_CIRCLE
                shape = ("circle" if circle else "rect", cx, cy, hw, hh)
                code = pad.GetNetCode() if pad.GetNetname() else BLOCKED
                layers = []
                if pad.IsOnLayer(pcbnew.F_Cu):
                    layers.append(0)
                if pad.IsOnLayer(pcbnew.B_Cu):
                    layers.append(1)
                npth = pad.GetAttribute() == pcbnew.PAD_ATTRIB_NPTH
                if npth:
                    layers = [0, 1]
                    code = BLOCKED
                inside = []
                for gx, gy, d in self._cells_near(shape, CLEAR + VIA_D / 2 + 0.01):
                    i = self.idx(gx, gy)
                    for l in layers:
                        if d < CLEAR + TRACK_W / 2 + MARGIN:
                            cur = self.occ[l][i]
                            self.occ[l][i] = code if cur in (FREE, code) else BLOCKED
                        if d < CLEAR + VIA_D / 2:
                            cur = self.via_ok[l][i]
                            self.via_ok[l][i] = code if cur in (FREE, code) else BLOCKED
                        # a via must not sit in a THT drill or on a pad edge;
                        # only allow it well inside nothing at all
                        if d <= 0 and npth:
                            self.via_ok[l][i] = BLOCKED
                        if d < -0.1 and code > 0:
                            inside.append((l, i))
                    # vias never inside pads
                    if d < VIA_D / 2 + 0.05 and code > 0:
                        for l in (0, 1):
                            self.via_ok[l][i] = BLOCKED
                # a THT pad also blocks vias on the layer it is not on (drill)
                if code > 0:
                    if not inside:
                        # pad centre to nearest grid cell
                        gx, gy = round(cx / GRID), round(cy / GRID)
                        inside = [(l, self.idx(gx, gy)) for l in layers]
                    self.pad_cells[(fp.GetReference(), pad.GetNumber())] = (code, inside, (cx, cy), layers)

    # -- routing ----------------------------------------------------------

    def _can_enter(self, l, i, code):
        o = self.occ[l][i]
        return o == FREE or o == code

    def _can_via(self, i, code):
        if i in self.vias:
            return True
        for l in (0, 1):
            if self.via_ok[l][i] not in (FREE, code):
                return False
            if self.occ[l][i] not in (FREE, code):
                return False
        # neighbouring cells must not carry another net's copper
        gx, gy = i % self.nx, i // self.nx
        for dx, dy in VIA_NEIGH:
            nx, ny = gx + dx, gy + dy
            if 0 <= nx < self.nx and 0 <= ny < self.ny:
                j = self.idx(nx, ny)
                for l in (0, 1):
                    o = self.occ[l][j]
                    if o not in (FREE, code, BLOCKED):
                        return False
        return True

    def route_net(self, code, terminals):
        """terminals: list of (layers, cells, centre). Returns True when all joined."""
        tree = set()  # (l, i)
        first = terminals[0]
        for c in first[1]:
            tree.add(c)
        remaining = list(terminals[1:])
        while remaining:
            targets = {}
            for t_index, t in enumerate(remaining):
                for c in t[1]:
                    targets[c] = t_index
            path = self._search(code, tree, targets)
            if path is None:
                return False
            hit = targets[path[-1]]
            self._commit(code, path)
            tree.update(path)
            for c in remaining[hit][1]:
                tree.add(c)
            remaining.pop(hit)
        return True

    def _search(self, code, sources, targets):
        nx = self.nx
        dist = {}
        prev = {}
        heap = []
        for s in sources:
            dist[s] = 0
            heapq.heappush(heap, (0, s))
        while heap:
            d, node = heapq.heappop(heap)
            if dist.get(node, 1 << 30) < d:
                continue
            if node in targets and node not in sources:
                path = [node]
                while path[-1] in prev:
                    path.append(prev[path[-1]])
                return path[::-1]
            l, i = node
            gx, gy = i % nx, i // nx
            steps = []
            if gx > 0:
                steps.append((l, i - 1, 1 if l == 1 else WRONG_WAY))
            if gx < nx - 1:
                steps.append((l, i + 1, 1 if l == 1 else WRONG_WAY))
            if gy > 0:
                steps.append((l, i - nx, 1 if l == 0 else WRONG_WAY))
            if gy < self.ny - 1:
                steps.append((l, i + nx, 1 if l == 0 else WRONG_WAY))
            if self._can_via(i, code):
                steps.append((1 - l, i, VIA_COST))
            for nl, ni, cost in steps:
                nnode = (nl, ni)
                if nl == l and not self._can_enter(nl, ni, code):
                    continue
                if nl != l and not self._can_enter(nl, ni, code):
                    continue
                nd = d + cost
                if nd < dist.get(nnode, 1 << 30):
                    dist[nnode] = nd
                    prev[nnode] = node
                    heapq.heappush(heap, (nd, nnode))
        return None

    def _commit(self, code, path):
        for k, (l, i) in enumerate(path):
            if self.occ[l][i] == FREE:
                self.occ[l][i] = code
            if k > 0:
                pl, pi = path[k - 1]
                if pl != l:
                    self._add_via(code, i)
                else:
                    x1, y1 = self.xy(pi)
                    x2, y2 = self.xy(i)
                    self.segments.append((l, x1, y1, x2, y2, code))

    def _add_via(self, code, i):
        if i in self.vias:
            return
        self.vias.add(i)
        x, y = self.xy(i)
        self.via_list.append((x, y, code))
        for l in (0, 1):
            self.occ[l][i] = code
        # keep other nets one cell away from the via
        gx, gy = i % self.nx, i // self.nx
        for dx, dy in VIA_NEIGH:
            nxx, nyy = gx + dx, gy + dy
            if 0 <= nxx < self.nx and 0 <= nyy < self.ny:
                j = self.idx(nxx, nyy)
                for l in (0, 1):
                    if self.occ[l][j] == FREE:
                        self.occ[l][j] = code

    # -- output -----------------------------------------------------------

    def write(self):
        mm = pcbnew.FromMM
        board = self.board
        # merge collinear grid steps into longer segments
        runs = {}
        for l, x1, y1, x2, y2, code in self.segments:
            a, b = sorted([(x1, y1), (x2, y2)])
            runs.setdefault((l, code), set()).add((a, b))
        for (l, code), segs in runs.items():
            for horiz in (True, False):
                lines = {}
                for a, b in segs:
                    if (a[1] == b[1]) == horiz:
                        key = a[1] if horiz else a[0]
                        lines.setdefault(key, []).append((a[0], b[0]) if horiz else (a[1], b[1]))
                for key, spans in lines.items():
                    spans.sort()
                    cur = list(spans[0])
                    merged = []
                    for s, e in spans[1:]:
                        if abs(s - cur[1]) < 1e-6:
                            cur[1] = e
                        else:
                            merged.append(cur)
                            cur = [s, e]
                    merged.append(cur)
                    for s, e in merged:
                        t = pcbnew.PCB_TRACK(board)
                        if horiz:
                            t.SetStart(pcbnew.VECTOR2I(mm(s), mm(key)))
                            t.SetEnd(pcbnew.VECTOR2I(mm(e), mm(key)))
                        else:
                            t.SetStart(pcbnew.VECTOR2I(mm(key), mm(s)))
                            t.SetEnd(pcbnew.VECTOR2I(mm(key), mm(e)))
                        t.SetWidth(mm(TRACK_W))
                        t.SetLayer(pcbnew.F_Cu if l == 0 else pcbnew.B_Cu)
                        t.SetNetCode(code)
                        board.Add(t)
        for x, y, code in self.via_list:
            v = pcbnew.PCB_VIA(board)
            v.SetPosition(pcbnew.VECTOR2I(mm(x), mm(y)))
            v.SetWidth(mm(VIA_D))
            v.SetDrill(mm(VIA_DRILL))
            v.SetNetCode(code)
            board.Add(v)


def stub(board, code, layer, centre, cell_xy):
    """Short track from a pad centre to the grid point the router used."""
    mm = pcbnew.FromMM
    (cx, cy), (gx, gy) = centre, cell_xy
    if abs(cx - gx) < 1e-6 and abs(cy - gy) < 1e-6:
        return
    t = pcbnew.PCB_TRACK(board)
    t.SetStart(pcbnew.VECTOR2I(mm(cx), mm(cy)))
    t.SetEnd(pcbnew.VECTOR2I(mm(gx), mm(gy)))
    t.SetWidth(mm(TRACK_W))
    t.SetLayer(pcbnew.F_Cu if layer == 0 else pcbnew.B_Cu)
    t.SetNetCode(code)
    board.Add(t)
