#!/usr/bin/env python3
"""Objective search for the correct map transform.

Score: % of robot positions (mm→cells) landing on cells with data.
Origin auto-alignment by bounding box + sweep.
"""
import base64, lzma, re, struct, zlib

LOG = 'data/captures/pairing-session.log'
MAP_ID = '273073661'
CELL = 50

# pass 1: spec
spec = None
for line in open(LOG, errors='replace'):
    m = re.search(r'<ctl c="(\d+)" h="(\d+)" i="(\d+)" id="[0-9a-f]+" m="([^"]+)" p="(\d+)" r="(\d+)" w="(\d+)"', line)
    if m and m.group(3) == MAP_ID:
        spec = [int(x) for x in m.group(4).split(',')]
if not spec:
    print("sin spec")
    sys.exit(1)
print(f"spec: {len(spec)} crcs")

# pass 2: CRC-valid pieces
pieces = {}
for line in open(LOG, errors='replace'):
    if 'td="MapP"' not in line:
        continue
    m3 = re.search(r'i="(\d+)"[^>]*p="([A-Za-z0-9+/=]{20,})"[^>]*pid="(\d+)"', line)
    if not m3 or m3.group(1) != MAP_ID:
        continue
    try:
        pid = int(m3.group(3))
        raw = base64.b64decode(m3.group(2))
        dec = lzma.decompress(raw[0:5] + struct.pack('<Q', 10000) + raw[9:], format=lzma.FORMAT_ALONE)
        if pid < len(spec) and zlib.crc32(dec) == spec[pid]:
            pieces[pid] = dec
    except Exception:
        pass
print(f"piezas CRC-validas: {len(pieces)}: {sorted(pieces)}")

# positions
pos = []
for line in open(LOG, errors='replace'):
    m = re.search(r'<ctl ([^>]*td="Pos"[^>]*)>', line)
    if not m:
        continue
    at = dict(re.findall(r'(\w+)="([^"]*)"', m.group(1)))
    if 'p' in at and ',' in at.get('p', ''):
        try:
            pos.append(tuple(float(v) for v in at['p'].split(',')))
        except ValueError:
            pass
print(f"posiciones: {len(pos)}")

TABLE = {18: (0, 0), 19: (0, 1), 20: (0, 2), 28: (0, 3),
         26: (1, 0), 27: (1, 2), 36: (1, 3),
         34: (2, 0), 35: (2, 1), 42: (2, 2), 43: (2, 3), 44: (3, 3)}


def build(placement, transpose):
    g = [[0] * 800 for _ in range(800)]
    for pid, dec in pieces.items():
        if placement == 'user':
            pr, pc = TABLE.get(pid, (99, 99))
        elif placement == 'rowmajor':
            pr, pc = pid // 8, pid % 8
        else:
            pr, pc = pid % 8, pid // 8
        r0, c0 = pr * 100, pc * 100
        for r in range(100):
            for c in range(100):
                v = dec[c * 100 + r] if transpose else dec[r * 100 + c]
                g[r0 + r][c0 + c] = v
    return g


def grid_bbox(g):
    rows = [r for r in range(800) if any(g[r])]
    cols = [c for c in range(800) if any(g[rr][c] for rr in range(800))]
    return (min(rows), max(rows), min(cols), max(cols)) if rows else None


results = []
for placement in ('rowmajor', 'user', 'colmajor'):
    for tp in (False, True):
        g = build(placement, tp)
        bbox = grid_bbox(g)
        if not bbox:
            continue
        rmin, rmax, cmin, cmax = bbox
        for sx in (1, -1):
            for sy in (1, -1):
                for swap in (False, True):
                    base_cells = []
                    for x, y in pos:
                        a, b = (y, x) if swap else (x, y)
                        base_cells.append((int(a * sx / CELL), int(b * sy / CELL)))
                    pxs = [c[0] for c in base_cells]
                    pys = [c[1] for c in base_cells]
                    # align trace center → map center, sweep ±20
                    pcx, pcy = (min(pxs) + max(pxs)) / 2, (min(pys) + max(pys)) / 2
                    gcx, gcy = (cmin + cmax) / 2, (rmin + rmax) / 2
                    ddx0, ddy0 = int(gcx - pcx), int(gcy - pcy)
                    best_local = (0.0, 0, 0)
                    for ddx in range(-20, 21, 4):
                        for ddy in range(-20, 21, 4):
                            dx, dy = ddx0 + ddx, ddy0 + ddy
                            hit = tot = 0
                            for x, y in base_cells:
                                c, r = x + dx, y + dy
                                if 0 <= r < 800 and 0 <= c < 800:
                                    tot += 1
                                    if g[r][c]:
                                        hit += 1
                            if tot and hit / tot > best_local[0]:
                                best_local = (hit / tot, dx, dy)
                    score, dx, dy = best_local
                    results.append((score, placement, tp, sx, sy, swap, dx, dy))
results.sort(reverse=True)
print('\nTOP 8:')
for r in results[:8]:
    print(f"  {r[0]*100:5.1f}%  colocacion={r[1]:9s} pieza_transp={r[2]}  ex={r[3]:+d} ey={r[4]:+d} swap={r[5]}  origen=({r[6]},{r[7]})")
