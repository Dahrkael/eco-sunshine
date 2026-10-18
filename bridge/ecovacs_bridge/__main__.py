"""Bridge CLI.

Commands:
  probe       - live read-only probe of a robot through Bumper (XMPP)
  probe-mqtt  - probe the eco-ng MQTT transport
  replay      - feed a captured corpus through the pipeline to MQTT
  serve       - live service: XMPP (Bumper) ⇄ MQTT (Home Assistant)
"""

import argparse
import sys

from . import __version__


def cmd_probe(args: argparse.Namespace) -> int:
    """Live probe: log in to Bumper and poll the robot read-only."""
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
        # poll the known robot directly (a docked robot pushes nothing on its own)
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


def cmd_replay(args: argparse.Namespace) -> int:
    """Replays a captured corpus through the full pipeline to MQTT (no robot involved)."""
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


def cmd_probe_mqtt(args: argparse.Namespace) -> int:
    """MQTT transport probe: connect to Bumper's broker, listen for
    reports and (optionally) send getBattery to the given did."""
    import asyncio
    import logging

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")

    from .config import load_config
    from .models import DeviceId
    from .mqtt_transport import MqttTransport

    cfg = load_config(args.config)
    bm = cfg.bumper_mqtt
    host = args.host or bm.host
    port = args.port or bm.port
    ca = args.ca_file or bm.ca_file

    seen: dict[str, str] = {}
    t: MqttTransport | None = None
    done = asyncio.Event()

    async def on_data(key: str, name: str, data: dict) -> None:
        if key not in seen:
            seen[key] = name
            print(f"🤖 robot eco-ng detectado: {key} (vía {name})")
        print(f"  [{name:>16s}] {data}")

    async def run() -> None:
        nonlocal t
        t = MqttTransport(host, port, ca_file=ca, on_data=on_data)
        await t.connect()
        print(f"conectado al broker {host}:{port} — escuchando {args.seconds}s")
        if args.did:
            did = DeviceId.from_key(args.did)
            print(f"sondeando {did.key}…")
            resp = await t.send_command(did, "getBattery", {})
            print(f"getBattery → {resp}")
            resp = await t.send_command(did, "getPos_V2", ["chargePos", "deebotPos"])
            print(f"getPos_V2  → {resp}")

        async def timer() -> None:
            await asyncio.sleep(args.seconds)
            done.set()

        asyncio.create_task(timer())
        listen = asyncio.create_task(t.listen())
        try:
            await done.wait()
        finally:
            listen.cancel()

    asyncio.run(run())
    print(f"\nrobots vistos: {len(seen)}")
    for key, via in seen.items():
        print(f"  {key} (primer frame: {via})")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """Live service: XMPP (Bumper) ⇄ MQTT (Home Assistant)."""
    import logging
    import os

    # DEEBOT_LOG_LEVEL wins (DEBUG reveals the full XMPP/MQTT wire)
    level = os.environ.get("DEEBOT_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s")
    # amqtt (embedded FC broker) logs its state machine at INFO — too noisy
    logging.getLogger("transitions").setLevel(logging.WARNING)

    from .config import load_config
    from .serve import Service

    cfg = load_config(args.config)
    if getattr(args, "mqtt", False):
        cfg.bumper_mqtt.enabled = True
    jid = args.jid or (cfg.robots[0] if cfg.robots else None)
    try:
        import asyncio

        asyncio.run(Service(cfg, jid).run())
    except KeyboardInterrupt:
        print("adiós")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="ecovacs-bridge")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_probe = sub.add_parser("probe", help="login en Bumper + sondeo read-only del robot")
    p_probe.add_argument("--config", "-c", default=None)
    p_probe.add_argument("--seconds", type=int, default=30)
    p_probe.add_argument("--jid", default=None, help="JID del robot (por defecto: conocido)")
    p_probe.set_defaults(fn=cmd_probe)

    p_replay = sub.add_parser("replay", help="corpus → pipeline → MQTT sin robot")
    p_replay.add_argument("--config", "-c", default=None)
    p_replay.add_argument("log", help="log de Bumper")
    p_replay.add_argument("extracted", help="dir extracted/ del corpus")
    p_replay.add_argument("--listen", type=int, default=10, help="segundos escuchando comandos")
    p_replay.set_defaults(fn=cmd_replay)

    p_serve = sub.add_parser("serve", help="servicio vivo XMPP → MQTT")
    p_serve.add_argument("--config", "-c", default=None)
    p_serve.add_argument("--jid", default=None)
    p_serve.add_argument("--mqtt", action="store_true",
                         help="activar transporte MQTT de Bumper (robots T8+)")
    p_serve.set_defaults(fn=cmd_serve)

    p_pmqtt = sub.add_parser("probe-mqtt", help="probar transporte MQTT del broker Bumper")
    p_pmqtt.add_argument("--config", "-c", default=None)
    p_pmqtt.add_argument("--host", default=None, help="host del broker (def: config)")
    p_pmqtt.add_argument("--port", type=int, default=None, help="puerto (def: 8883)")
    p_pmqtt.add_argument("--ca-file", default=None, help="CA del broker (def: config)")
    p_pmqtt.add_argument("--did", default=None, help="did@class/res a sondear con getBattery")
    p_pmqtt.add_argument("--seconds", type=int, default=10)
    p_pmqtt.add_argument("-v", "--verbose", action="store_true")
    p_pmqtt.set_defaults(fn=cmd_probe_mqtt)

    args = parser.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
