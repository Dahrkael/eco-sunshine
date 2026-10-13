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
    """Prueba en vivo: login en Bumper, auto-descubrir bot, sondeo read-only."""
    import asyncio

    from .config import load_config
    from .robot import RobotCommander, RobotState
    from .xmpp_client import BumperXMPPClient

    cfg = load_config(args.config)
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


def cmd_serve(args: argparse.Namespace) -> int:
    print("TODO(fase 4): servicio XMPP -> MQTT")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="deebot930-bridge")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("tail").set_defaults(fn=cmd_tail)

    p_probe = sub.add_parser("probe", help="login en Bumper + sondeo read-only del robot")
    p_probe.add_argument("--config", "-c", default=None)
    p_probe.add_argument("--seconds", type=int, default=30)
    p_probe.set_defaults(fn=cmd_probe)

    p_decode = sub.add_parser("decode")
    p_decode.add_argument("trace", help="fichero de traza XMPP capturada")
    p_decode.set_defaults(fn=cmd_decode)

    sub.add_parser("serve").set_defaults(fn=cmd_serve)

    args = parser.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
