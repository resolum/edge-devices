"""python -m alivia_edge --config config.toml"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from alivia_edge.shared.config import load_config
from alivia_edge.voice.interfaces.server import EdgeServer
from alivia_edge.shared.infrastructure.integrity import IntegrityError
from alivia_edge.app import build_services, preflight, start_services, stop_services


async def _run(cfg, check_only: bool) -> None:
    problems = preflight(cfg)
    if problems:
        sys.exit("No se puede arrancar:\n  - " + "\n  - ".join(problems) + "\n\nDescarga los modelos con:  python scripts/fetch_models.py")
    svc = build_services(cfg)
    try:
        await start_services(svc)
    except IntegrityError as exc:
        await stop_services(svc)
        sys.exit(f"No se puede arrancar: {exc}")
    if check_only:
        logging.getLogger("alivia").info("Verificación completa: modelos y servicios cargan correctamente.")
        await stop_services(svc)
        return
    server = EdgeServer(svc)
    await server.start()
    logging.getLogger("alivia").info("Listo. Esperando nodos (Ctrl+C para salir).")
    try:
        await asyncio.Event().wait()
    finally:
        await server.stop()
        await stop_services(svc)


def main() -> None:
    ap = argparse.ArgumentParser(prog="alivia-edge", description="Edge de voz de Alivia")
    ap.add_argument("--config", default="config.toml")
    ap.add_argument("--check", action="store_true", help="carga modelos y conexiones, y sale")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()

    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        cfg = load_config(args.config)
    except (OSError, ValueError) as exc:
        sys.exit(f"Configuración inválida: {exc}")
    try:
        asyncio.run(_run(cfg, args.check))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
