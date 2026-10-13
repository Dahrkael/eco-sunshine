#!/usr/bin/env python3
"""Extrae del log de Bumper los frames XMPP del robot a ficheros estructurados.

Uso: python3 tools/extract_corpus.py <log> <outdir>
Genera: positions.jsonl, traces.jsonl, mapm.jsonl, other.jsonl, resumen.tsv
"""

import base64
import json
import re
import sys
from collections import Counter
from pathlib import Path

FRAME_RE = re.compile(
    r"^(\S+Z) .*_parse_data.*from \(172[^\)]*\| (?P<jid>[^)]+)\) - (?P<xml>.+)$"
)
CTL_RE = re.compile(r'<ctl\s+(.*?)/>')
ATTR_RE = re.compile(r'(\w+)="([^"]*)"')


def parse_attrs(s: str) -> dict:
    return dict(ATTR_RE.findall(s))


def main(log: str, outdir: str) -> None:
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    files = {
        "positions.jsonl": open(out / "positions.jsonl", "w"),
        "traces.jsonl": open(out / "traces.jsonl", "w"),
        "mapm.jsonl": open(out / "mapm.jsonl", "w"),
        "other.jsonl": open(out / "other.jsonl", "w"),
    }
    counts: Counter = Counter()

    with open(log, errors="replace") as f:
        for line in f:
            if "_parse_data" not in line:
                continue
            m = FRAME_RE.match(line)
            if not m:
                continue
            ts, jid, xml = m.group(1), m.group("jid"), m.group("xml")
            for ctl in CTL_RE.findall(xml):
                attrs = parse_attrs(ctl)
                td = attrs.get("td", "?")
                counts[td] += 1
                rec = {"ts": ts, "from_bot": "ecorobot.net" in jid, **attrs}
                dest = "other.jsonl"
                if td == "Pos":
                    dest = "positions.jsonl"
                elif td == "trace":
                    if "tr" in attrs:
                        try:
                            raw = base64.b64decode(attrs["tr"])
                            rec["tr_hex"] = raw.hex()
                            rec["tr_len"] = len(raw)
                        except Exception as e:  # noqa: BLE001
                            rec["tr_err"] = str(e)
                    dest = "traces.jsonl"
                elif td in ("GetMapM", "MapSet", "GetMapSet", "MapSt", "TrM", "PullMP") or "p" in attrs:
                    if "p" in attrs and attrs["p"].startswith("XQ"):
                        raw = base64.b64decode(attrs["p"])
                        rec["p_hex"] = raw.hex()
                        rec["p_len"] = len(raw)
                        del rec["p"]
                    dest = "mapm.jsonl"
                files[dest].write(json.dumps(rec) + "\n")

    for f in files.values():
        f.close()

    with open(out / "resumen.tsv", "w") as r:
        for td, n in counts.most_common():
            r.write(f"{td}\t{n}\n")
    print("Frames por td:")
    for td, n in counts.most_common(12):
        print(f"  {td}: {n}")
    print(f"\nEscrito en {out}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
