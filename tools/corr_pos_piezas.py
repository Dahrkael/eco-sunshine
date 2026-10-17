#!/usr/bin/env python3
"""Correlates positions (mm) vs MapP pieces to determine placement."""
import base64, lzma, re, struct, itertools, sys

LOG = 'data/captures/pairing-session.log'
MAP_ID = '273073661'

seen = {}
for line in open(LOG, errors='replace'):
    if 'td="MapP"' not in line:
        continue
    m = re.search(r'i="(\d+)"[^>]*p="([A-Za-z0-9+/=]{20,})"[^>]*pid="(\d+)"', line)
    if not m or m.group(1) != MAP_ID:
        continue
    try:
        raw = base64.b64decode(m.group(2))
        piece = lzma.decompress(raw[0:5] + struct.pack('<Q', 10000) + raw[9:], format=lzma.FORMAT_ALONE)
        seen[int(m.group(3))] = piece
    except Exception:
        continue

pos = []
for line in open(LOG, errors='replace'):
    m = re.search(r'<ctl ([^>]*td="Pos"[^>]*)>', line)
    if not m:
        continue
    attrs = dict(re.findall(r'(\w+)="([^"]*)"', m.group(1)))
    if 'p' in attrs and ',' in attrs['p']:
        try:
            x, y = (float(v) for v in attrs['p'].split(','))
            pos.append((x, y))
        except ValueError:
            pass

print(f"{len(seen)} piezas: {sorted(seen)}")
print(f"{len(pos)} posiciones")

def build(order):
    g = [[0] * 800 for _ in range(800)]
    for pid, piece in seen.items():
        if order == 'fila':
            r0, c0 = (pid // 8) * 100, (pid % 8) * 100
        else:
            r0, c0 = (pid % 8) * 100, (pid // 8) * 100
        for r in range(100):
            for c in range(100):
                g[r0 + r][c0 + c] = piece[r * 100 + c]
    return g

results = []
for order in ('fila', 'columna'):
    grid = build(order)
    for sx in (1, -1):
        for sy in (1, -1):
            for swap in (False, True):
                hit = total = 0
                for x, y in pos:
                    cx, cy = (int(y * sx / 50), int(x * sy / 50)) if swap else (int(x * sx / 50), int(y * sy / 50))
                    if 0 <= cx < 800 and 0 <= cy < 800:
                        total += 1
                        if grid[cy][cx] != 0:
                            hit += 1
                if total:
                    results.append((hit / total, order, sx, sy, swap, total))
results.sort(reverse=True)
print('\nTop 6 (traza sobre suelo):')
for r in results[:6]:
    print(f"  {r[0]*100:5.1f}%  orden={r[1]:8s} ex={r[2]:+d} ey={r[3]:+d} swap={r[4]}  ({r[5]} pts)")
print('Peor:', f"{results[-1][0]*100:.1f}%  orden={results[-1][1]}")
