"""Komendy Etapu 2: test łączności z Krakenem i wycena portfela (tylko odczyt)."""

from __future__ import annotations

from krakenbot.config import RetryConfig, Settings
from krakenbot.db import audit, connect, transaction
from krakenbot.kraken_client import KrakenClient, KrakenError, Transport, make_transport
from krakenbot.logging_setup import setup_logging
from krakenbot.portfolio import format_valuation, pairs_to_price, save_snapshot, value_portfolio
from krakenbot.retry import RetryExhaustedError, TransientError

# Podpowiedzi dla najczęstszych błędów klucza. Kraken przy zapytaniu spoza whitelisty IP
# zwraca błąd klucza lub uprawnień, więc pokazujemy obie możliwe przyczyny.
_HINTS = {
    "EAPI:Invalid key": "zły klucz albo zapytanie z adresu IP spoza whitelisty klucza",
    "EAPI:Invalid signature": "zły sekret (KRAKEN_READ_API_SECRET) albo uszkodzony przy kopiowaniu",
    "EAPI:Invalid nonce": "nonce mniejszy niż poprzedni: ten sam klucz używany w dwóch miejscach?",
    "EGeneral:Permission denied": "klucz nie ma uprawnienia albo IP spoza whitelisty",
    "EGeneral:Temporary lockout": "klucz czasowo zablokowany po błędach, odczekaj kilka minut",
}


def make_read_client(
    settings: Settings,
    transport: Transport | None = None,
    retry_cfg: RetryConfig | None = None,
) -> KrakenClient:
    s = settings.secrets
    key = s.kraken_read_key.get_secret_value() if s.kraken_read_key else None
    secret = s.kraken_read_secret.get_secret_value() if s.kraken_read_secret else None
    return KrakenClient(
        settings.kraken,
        retry_cfg or settings.retry,
        api_key=key,
        api_secret=secret,
        transport=transport or make_transport(settings.kraken.ip_version),
    )


def explain(exc: KrakenError) -> str:
    hints = [h for code, h in _HINTS.items() if any(e.startswith(code) for e in exc.errors)]
    return str(exc) + (f" ({'; '.join(hints)})" if hints else "")


def check_kraken(
    settings: Settings,
    transport: Transport | None = None,
    *,
    ipv6_negative_test: bool = False,
    ipv6_transport: Transport | None = None,
) -> int:
    """Sprawdza API publiczne, a jeśli jest klucz, także prywatne (saldo). 0 = OK.

    Z ipv6_negative_test=True dodatkowo wysyła to samo zapytanie przez IPv6, czyli z adresu
    spoza whitelisty. Test zalicza się tylko wtedy, gdy Kraken je ODRZUCI.
    """
    log = setup_logging(settings)
    client = make_read_client(settings, transport)
    mode = settings.mode.value

    status = client.system_status()
    server_time = client.server_time()
    log.info("Kraken publiczne API OK (IPv%d): status=%s, czas serwera=%d",
             settings.kraken.ip_version, status, server_time)

    missing = settings.secrets.missing("kraken_read_key", "kraken_read_secret")
    if missing:
        log.warning("Pomijam test prywatnego API, brak: %s", ", ".join(missing))
        return 1 if ipv6_negative_test else 0

    conn = connect(settings.paths.db_path)
    try:
        try:
            balances = client.balance()
        except KrakenError as exc:
            log.error("Kraken prywatne API ODRZUCIŁO zapytanie: %s", explain(exc))
            with transaction(conn):
                audit(conn, mode=mode, category="system", event="kraken_auth_failed",
                      details={"errors": exc.errors})
            return 1
        log.info("Kraken prywatne API OK: %d pozycji w saldzie", len(balances))
        with transaction(conn):
            audit(conn, mode=mode, category="system", event="kraken_auth_ok",
                  details={"kraken_status": status, "assets": sorted(balances)})

        if not ipv6_negative_test:
            return 0
        result = _ipv6_negative(settings, ipv6_transport, log)
        with transaction(conn):
            audit(conn, mode=mode, category="system", event="kraken_ip_whitelist_test",
                  details={"result": result})
        return 0 if result == "rejected" else 1
    finally:
        conn.close()


def _ipv6_negative(settings: Settings, transport: Transport | None, log) -> str:
    client = make_read_client(
        settings, transport or make_transport(6), RetryConfig(max_attempts=1)
    )
    try:
        client.balance()
    except KrakenError as exc:
        log.info("Test whitelisty ZALICZONY: zapytanie z adresu IPv6 odrzucone (%s)",
                 ", ".join(exc.errors))
        return "rejected"
    except (TransientError, RetryExhaustedError) as exc:
        log.warning("Test whitelisty NIEWYKONALNY: brak połączenia przez IPv6 (%s)", exc)
        return "no_ipv6"
    log.error("Test whitelisty NIEZALICZONY: Kraken PRZYJĄŁ zapytanie spoza adresu IPv4. "
              "Sprawdź, czy whitelista klucza jest włączona i zawiera tylko adres serwera.")
    return "accepted"


def portfolio(settings: Settings, transport: Transport | None = None) -> int:
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
