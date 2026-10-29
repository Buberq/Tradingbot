"""Punkt wejścia bota.

Etap 1: `krakenbot start` ładuje konfigurację, ustawia logowanie, przygotowuje bazę,
raportuje tryb pracy i zapisuje wpis startowy do audytu. Pętla orkiestratora dojdzie
w Etapie 10.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from krakenbot import __version__
from krakenbot.config import ConfigError, Mode, Settings, load_settings
from krakenbot.db import SCHEMA_VERSION, audit, connect, get_state, transaction
from krakenbot.logging_setup import setup_logging

PAUSED_KEY = "paused"


def load_dotenv(path: Path = Path(".env"), env: dict[str, str] | None = None) -> int:
    """Minimalny loader pliku .env. Nie nadpisuje zmiennych już ustawionych w środowisku."""
    env = os.environ if env is None else env
    if not path.is_file():
        return 0
    loaded = 0
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip().removeprefix("export ").strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and value and key not in env:
            env[key] = value
            loaded += 1
    return loaded


def startup(settings: Settings) -> int:
    """Start bota. Zwraca id wpisu startowego w audit_log."""
    log = setup_logging(settings)
    mode = settings.mode.value
    if settings.mode is Mode.LIVE:
        log.warning("TRYB LIVE: bot składa PRAWDZIWE zlecenia na Krakenie")
    else:
        log.info("Tryb PAPER: symulacja na żywych cenach, zlecenia tylko w bazie")

    conn = connect(settings.paths.db_path)
    try:
        paused = get_state(conn, PAUSED_KEY, "0") == "1"
        with transaction(conn):
            entry_id = audit(
                conn,
                mode=mode,
                category="system",
                event="startup",
                details={
                    "version": __version__,
                    "schema_version": SCHEMA_VERSION,
                    "paused": paused,
                    "risk": settings.risk.model_dump(),
                    "secrets_present": sorted(
                        k for k, v in settings.secrets.__dict__.items() if v is not None
                    ),
                },
            )
    finally:
        conn.close()

    log.info(
        "Start OK: wersja %s, schemat bazy v%d, wpis audytu #%d, pauza=%s",
        __version__, SCHEMA_VERSION, entry_id, "TAK" if paused else "nie",
    )
    if paused:
        log.warning("Handel jest wstrzymany (flaga w bazie). Wznowienie tylko ręcznie: /resume")
    return entry_id


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="krakenbot", description="Bot tradingowy Kraken")
    parser.add_argument("--config", help="ścieżka do config.yaml")
    parser.add_argument("--env-file", default=".env", help="plik ze zmiennymi (domyślnie .env)")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("start", help="uruchom bota (Etap 1: kontrola startu i wpis do bazy)")
    sub.add_parser("check-config", help="sprawdź konfigurację i zakończ")
    args = parser.parse_args(argv)

    load_dotenv(Path(args.env_file))
    try:
        settings = load_settings(args.config)
    except ConfigError as exc:
        print(f"BŁĄD KONFIGURACJI: {exc}", file=sys.stderr)
        return 2

    if args.command == "check-config":
        print(f"Konfiguracja poprawna. Tryb: {settings.mode.value}")
        missing = settings.secrets.missing(*type(settings.secrets).model_fields)
        if missing:
            print("Brakujące sekrety (potrzebne w kolejnych etapach): " + ", ".join(missing))
        return 0

    startup(settings)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(cli())
