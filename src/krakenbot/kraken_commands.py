"""Komendy Etapu 2: test łączności z Krakenem i wycena portfela (tylko odczyt)."""

from __future__ import annotations

from krakenbot.config import Settings
from krakenbot.db import audit, connect, transaction
from krakenbot.kraken_client import KrakenClient, KrakenError, Transport, urllib_transport
from krakenbot.logging_setup import setup_logging
from krakenbot.portfolio import format_valuation, pairs_to_price, save_snapshot, value_portfolio

# Podpowiedzi dla najczęstszych błędów klucza. Kraken przy zapytaniu spoza whitelisty IP
# zwraca błąd klucza lub uprawnień, więc pokazujemy obie możliwe przyczyny.
_HINTS = {
    "EAPI:Invalid key": "zły klucz albo zapytanie z adresu IP spoza whitelisty klucza",
    "EAPI:Invalid signature": "zły sekret (KRAKEN_READ_API_SECRET) albo uszkodzony przy kopiowaniu",
    "EAPI:Invalid nonce": "nonce mniejszy niż poprzedni: ten sam klucz używany w dwóch miejscach?",
    "EGeneral:Permission denied": "klucz nie ma uprawnienia albo IP spoza whitelisty",
    "EGeneral:Temporary lockout": "klucz czasowo zablokowany po błędach, odczekaj kilka minut",
}


def make_read_client(settings: Settings, transport: Transport = urllib_transport) -> KrakenClient:
    s = settings.secrets
    key = s.kraken_read_key.get_secret_value() if s.kraken_read_key else None
    secret = s.kraken_read_secret.get_secret_value() if s.kraken_read_secret else None
    return KrakenClient(
        settings.kraken, settings.retry, api_key=key, api_secret=secret, transport=transport
    )


def explain(exc: KrakenError) -> str:
    hints = [h for code, h in _HINTS.items() if any(e.startswith(code) for e in exc.errors)]
    return str(exc) + (f" ({'; '.join(hints)})" if hints else "")


def check_kraken(settings: Settings, transport: Transport = urllib_transport) -> int:
    """Sprawdza API publiczne, a jeśli jest klucz, także prywatne (saldo). 0 = OK."""
    log = setup_logging(settings)
    client = make_read_client(settings, transport)
    mode = settings.mode.value

    status = client.system_status()
    server_time = client.server_time()
    log.info("Kraken publiczne API OK: status=%s, czas serwera=%d", status, server_time)

    missing = settings.secrets.missing("kraken_read_key", "kraken_read_secret")
    if missing:
        log.warning("Pomijam test prywatnego API, brak: %s", ", ".join(missing))
        return 0

    conn = connect(settings.paths.db_path)
    try:
        try:
            balances = client.balance()
        except KrakenError as exc:
            msg = explain(exc)
            log.error("Kraken prywatne API ODRZUCIŁO zapytanie: %s", msg)
            with transaction(conn):
                audit(conn, mode=mode, category="system", event="kraken_auth_failed",
                      details={"errors": exc.errors})
            return 1
        log.info("Kraken prywatne API OK: %d pozycji w saldzie", len(balances))
        with transaction(conn):
            audit(conn, mode=mode, category="system", event="kraken_auth_ok",
                  details={"kraken_status": status, "assets": sorted(balances)})
    finally:
        conn.close()
    return 0


def portfolio(settings: Settings, transport: Transport = urllib_transport) -> int:
    """Pobiera salda i ceny, wycenia portfel w EUR i zapisuje snapshot. 0 = OK."""
    log = setup_logging(settings)
    client = make_read_client(settings, transport)
    try:
        balances = client.balance()
    except KrakenError as exc:
        log.error("Nie udało się pobrać salda: %s", explain(exc))
        return 1
    pairs = client.asset_pairs()
    tickers = client.ticker(pairs_to_price(balances, pairs))
    valuation = value_portfolio(balances, pairs, tickers)

    conn = connect(settings.paths.db_path)
    try:
        with transaction(conn):
            snap_id = save_snapshot(conn, mode=settings.mode.value, valuation=valuation,
                                    source="kraken")
            audit(conn, mode=settings.mode.value, category="system", event="portfolio_snapshot",
                  details={"snapshot_id": snap_id, "total_eur": str(valuation.total_eur)})
    finally:
        conn.close()
    for line in format_valuation(valuation).splitlines():
        log.info(line)
    if valuation.unpriced:
        log.warning("Część aktywów nie ma pary do EUR i nie jest wliczona do wartości")
    return 0
