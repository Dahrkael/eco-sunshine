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

    p_decode = sub.add_parser("decode")
    p_decode.add_argument("trace", help="fichero de traza XMPP capturada")
    p_decode.set_defaults(fn=cmd_decode)

    sub.add_parser("serve").set_defaults(fn=cmd_serve)

    args = parser.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
