#!/usr/bin/env python3
"""Correlacion completa: colocacion de piezas + ejes + offset de origen.

Las posiciones (mm) y el grid (celdas) tienen origenes distintos: se resuelve
alineando bounding boxes y barriendo offsets alrededor.
"""
import base64, lzma, re, struct, zlib

LOG = 'data/captures/pairing-session.log'
MAP_ID = '273073661'
CELL = 50  # mm por celda

# --- piezas ---
best = {}
for line in open(LOG, errors='replace'):
    if 'td="MapP"' not in line:
        continue
    m = re.search(r'i="(\d+)"[^>]*p="([A-Za-z0-9+/=]{20,})"[^>]*pid="(\d+)"', line)
    if not m or m.group(1) != MAP_ID:
        continue
    try:
        raw = base64.b64decode(m.group(2))
        piece = lzma.decompress(raw[0:5] + struct.pack('<Q', 10000) + raw[9:], format=lzma.FORMAT_ALONE)
        best[int(m.group(3))] = piece
    except Exception:
        continue

# --- posiciones (unicas en celdas base) ---
pos_cells = set()
for line in open(LOG, errors='replace'):
    m = re.search(r'<ctl ([^>]*td="Pos"[^>]*)>', line)
    if not m:
        continue
    at = dict(re.findall(r'(\w+)="([^"]*)"', m.group(1)))
    if 'p' in at and ',' in at['p']:
        try:
            x, y = (float(v) for v in at['p'].split(','))
            pos_cells.add((int(x // CELL), int(y // CELL)))
        except (ValueError, ZeroDivisionError):
            pass
print(f"{len(best)} piezas, {len(pos_cells)} celdas unicas de traza")

def build(order):
    g = [[0] * 800 for _ in range(800)]
    for pid, piece in best.items():
        if order == 'fila':
            r0, c0 = (pid // 8) * 100, (pid % 8) * 100
        else:
            r0, c0 = (pid % 8) * 100, (pid // 8) * 100
        for r in range(100):
            for c in range(100):
                g[r0 + r][c0 + c] = piece[r * 100 + c]
    # bbox de contenido
    rows = [r for r in range(800) if any(g[r])]
    cols = [c for c in range(800) if any(g[r][c] for r in range(800))]
    return g, min(rows), max(rows), min(cols), max(cols)

def cloud_bbox(cells):
    xs = [c[0] for c in cells]
    ys = [c[1] for c in cells]
    return min(xs), max(xs), min(ys), max(ys)

cx0, cx1, cy0, cy1 = cloud_bbox(pos_cells)
print(f"bbox traza: x {cx0}..{cx1}, y {cy0}..{cy1}")

results = []
for order in ('fila', 'columna'):
    g, rmin, rmax, cmin, cmax = build(order)
    for sx in (1, -1):
        for sy in (1, -1):
            for swap in (False, True):
                # bbox de la traza transformada
                pts = set()
                for x, y in pos_cells:
                    cx, cy = (y * sx, x * sy) if swap else (x * sx, y * sy)
                    pts.add((cx, cy))
                if not pts:
                    continue
                pxs = [p[0] for p in pts]
                pys = [p[1] for p in pts]
                dx0 = min(rows for rows in [rmin]) - min(pxs)
                dy0 = rmin - min(pys)
                # alinear bbox traza → bbox mapa, y barrer ±30 celdas
                best_local = (0.0, dx0, dy0)
                for ddx in range(-30, 31, 5):
                    for ddy in range(-30, 31, 5):
                        dx, dy = dx0 + ddx, dy0 + ddy
                        hit = tot = 0
                        for x, y in pts:
                            c, r = x + dx, y + dy
                            if 0 <= c < 800 and 0 <= r < 800:
                                tot += 1
                                if g[r][c]:
                                    hit += 1
                        if tot and hit / tot > best_local[0]:
                            best_local = (hit / tot, dx, dy)
                score, dx, dy = best_local
                results.append((score, order, sx, sy, swap, dx, dy))
                print(f"  {order} ex={sx:+d} ey={sy:+d} swap={swap}: {score*100:.1f}% (offset {dx},{dy})")

results.sort(reverse=True)
print("\nGANADORA:", results[0])
