"""CLI del bridge. De momento solo valida el layout del paquete.

Comandos previstos:
  tail     - seguir el log XMPP de Bumper (fase 2: captura de frames)
  decode   - decodificar una traza guardada a eventos estructurados (fase 3)
  serve    - modo servicio: XMPP -> MQTT (fase 4)
"""

import argparse
import sys

from . import __version__


def cmd_tail(args: argparse.Namespace) -> int:
    print("TODO(fase 2): tail del log de Bumper (docker logs deebot930-bumper)")
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    """Prueba en vivo: login en Bumper, sondeo read-only del robot."""
    import asyncio
    import logging

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    from .config import load_config
    from .robot import RobotCommander, RobotState
    from .xmpp_client import BumperXMPPClient

    KNOWN_JID = "E000105701860607AB7@115.ecorobot.net/atom"
    cfg = load_config(args.config)
    jid_hint = args.jid or (cfg.robots[0] if cfg.robots else KNOWN_JID)

    robots: dict[str, RobotState] = {}
    commanders: dict[str, RobotCommander] = {}
    done = asyncio.Event()

    async def on_event(jid: str, attrs: dict, inner: str) -> None:
        if "ecorobot.net" in jid and jid not in robots:
            r = RobotState.from_jid(jid)
            robots[jid] = r
            commanders[jid] = RobotCommander(client, r)
            print(f"🤖 robot descubierto: {jid} (slug {r.slug})")
            c = commanders[jid]
            c.get_pos(); c.get_clean_state(); c.get_charge_state()
            c.get_lifespan("Brush"); c.get_version()
        r = robots.get(jid)
        if r:
            r.update(attrs, inner)
            td = attrs.get("td", "?")
            pos = f" pos={r.position.x:.0f},{r.position.y:.0f}@{r.position.angle:.0f}°" if r.position else ""
            print(f"  [{td:>10s}] {attrs}{pos}")
        else:
            print(f"  [server  ] {attrs}")

    async def timer() -> None:
        await asyncio.sleep(getattr(args, "seconds", 30))
        done.set()

    client: BumperXMPPClient | None = None

    async def run() -> None:
        nonlocal client
        from .config import BumperConfig

        b: BumperConfig = cfg.bumper
        client = BumperXMPPClient(
            host=b.host, port=b.port, uid=b.uid, resource=b.resource,
            ca_file=b.ca_file, on_event=on_event,
        )
        await client.connect()
        # sondeo directo al robot conocido (dormido no emite nada solo)
        if jid_hint:
            r = RobotState.from_jid(jid_hint)
            robots[jid_hint] = r
            commanders[jid_hint] = RobotCommander(client, r)
            c = commanders[jid_hint]
            c.get_pos(); c.get_clean_state(); c.get_charge_state()
            c.get_lifespan("Brush"); c.get_version()
            print(f"🤖 sondeando {jid_hint}")
        asyncio.create_task(timer())
        listen_task = asyncio.create_task(client.listen())
        try:
            await done.wait()
        finally:
            listen_task.cancel()

    asyncio.run(run())
    for r in robots.values():
        print(f"\n=== {r.slug}: status={r.status} battery={r.battery} pos={r.position} ===")
    return 0


def cmd_decode(args: argparse.Namespace) -> int:
    print(f"TODO(fase 3): decodificar traza {args.trace}")
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    """Reproduce el corpus por el pipeline completo → MQTT (sin tocar el robot)."""
    import asyncio
    import json as jsonlib
    import logging
    import re
    from pathlib import Path

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    from .config import load_config
    from .map_pieces import MapAssembler, render_png
    from .mqtt_pub import MqttPublisher
    from .robot import RobotState

    cfg = load_config(args.config)

    async def run() -> None:
        asm = MapAssembler()
        ctl_re = re.compile(r"<ctl\s+([^>]*)>")
        attr_re = re.compile(r'(\w+)="([^"]*)"')
        seen, counters = set(), {}
        for line in open(args.log, errors="replace"):
            for tag in ctl_re.findall(line):
                at = dict(attr_re.findall(tag))
                p, i = at.get("p", ""), at.get("i", "")
                if i and len(p) > 20 and p not in seen and p.startswith("XQ"):
                    seen.add(p)
                    pid = counters.get(i, 0)
                    counters[i] = pid + 1
                    asm.add_piece(i, pid, p)

        robot = RobotState(jid="replay", slug="deebot-replay")
        pos_events = []
        pos_file = Path(args.extracted) / "positions.jsonl"
        if pos_file.exists():
            for line in open(pos_file):
                ev = jsonlib.loads(line)
                pos_events.append(ev)
                robot.update(ev, "")

        pub = MqttPublisher(cfg.mqtt.host, cfg.mqtt.port, cfg.mqtt.user,
                            cfg.mqtt.password, cfg.mqtt.base_topic)
        await pub.connect()
        await pub.publish_discovery(robot.slug, "Deebot (replay)")
        await pub.publish_state(robot.slug, robot.status)
        await pub.publish_attributes(robot.slug, {
            "posiciones": len(pos_events),
            "consumibles": robot.lifespans,
            "mapa_ids": list(asm.maps),
        })

        if asm.maps:
            last_map = max(asm.maps, key=lambda m: asm.piece_count(m))
            png = render_png(asm.assemble(last_map))
            await pub.publish_map(robot.slug, png)
            print(f"mapa {last_map}: {asm.piece_count(last_map)} piezas, {len(pos_events)} pos")

        async def on_cmd(slug: str, action: str, val) -> None:
            print(f"comando MQTT recibido: {slug} {action}")

        pub.on_command = on_cmd
        try:
            await asyncio.wait_for(pub.listen_commands(), timeout=args.listen)
        except asyncio.TimeoutError:
            pass
        print("replay completo")

    asyncio.run(run())
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """Servicio vivo: XMPP (Bumper) ⇄ MQTT (Home Assistant)."""
    import logging

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    from .config import load_config
    from .serve import Service

    cfg = load_config(args.config)
    jid = args.jid or (cfg.robots[0] if cfg.robots else None)
    try:
        import asyncio

        asyncio.run(Service(cfg, jid).run())
    except KeyboardInterrupt:
        print("adiós")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="deebot930-bridge")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("tail").set_defaults(fn=cmd_tail)

    p_probe = sub.add_parser("probe", help="login en Bumper + sondeo read-only del robot")
    p_probe.add_argument("--config", "-c", default=None)
    p_probe.add_argument("--seconds", type=int, default=30)
    p_probe.add_argument("--jid", default=None, help="JID del robot (por defecto: conocido)")
    p_probe.set_defaults(fn=cmd_probe)

    p_decode = sub.add_parser("decode")
    p_decode.add_argument("trace", help="fichero de traza XMPP capturada")
    p_decode.set_defaults(fn=cmd_decode)

    p_replay = sub.add_parser("replay", help="corpus → pipeline → MQTT sin robot")
    p_replay.add_argument("--config", "-c", default=None)
    p_replay.add_argument("log", help="log de Bumper")
    p_replay.add_argument("extracted", help="dir extracted/ del corpus")
    p_replay.add_argument("--listen", type=int, default=10, help="segundos escuchando comandos")
    p_replay.set_defaults(fn=cmd_replay)

    p_serve = sub.add_parser("serve", help="servicio vivo XMPP → MQTT")
    p_serve.add_argument("--config", "-c", default=None)
    p_serve.add_argument("--jid", default=None)
    p_serve.set_defaults(fn=cmd_serve)

    args = parser.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
