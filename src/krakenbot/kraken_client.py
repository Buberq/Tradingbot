"""Klient REST API Kraken (rynek SPOT).

Bezpieczeństwo:
- tylko HTTPS z weryfikacją certyfikatu i nazwy hosta,
- zapytania prywatne podpisane HMAC-SHA512 zgodnie z dokumentacją Krakena;
  sekret nigdy nie jest wysyłany ani logowany,
- metody wypłat i transferów są zablokowane w kodzie dla każdego klucza,
- klient z kluczem tylko do odczytu odrzuca każdą metodę spoza listy odczytu,
  zanim zapytanie wyjdzie z serwera (druga warstwa obok uprawnień klucza na Krakenie).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from krakenbot.config import KrakenConfig, RetryConfig
from krakenbot.logging_setup import get_logger
from krakenbot.retry import PermanentError, TransientError, retry

log = get_logger("kraken")

API_VERSION = "0"

# Metody, których bot nie wywoła nigdy, niezależnie od klucza i konfiguracji.
FORBIDDEN_PRIVATE_METHODS: frozenset[str] = frozenset(
    {
        "Withdraw",
        "WithdrawCancel",
        "WithdrawInfo",
        "WithdrawMethods",
        "WithdrawAddresses",
        "WalletTransfer",
        "DepositAddresses",
        "CreateSubaccount",
        "AccountTransfer",
        "Stake",
        "Unstake",
        "AddExport",
        "RemoveExport",
    }
)

# Metody dozwolone dla klucza tylko do odczytu (Etap 2).
READ_ONLY_PRIVATE_METHODS: frozenset[str] = frozenset(
    {
        "Balance",
        "BalanceEx",
        "TradeBalance",
        "OpenOrders",
        "ClosedOrders",
        "QueryOrders",
        "TradesHistory",
        "QueryTrades",
        "Ledgers",
        "QueryLedgers",
        "TradeVolume",
    }
)

# Metody handlowe, dozwolone tylko dla klucza handlowego (od Etapu 4).
TRADE_PRIVATE_METHODS: frozenset[str] = frozenset(
    {"AddOrder", "CancelOrder", "CancelAll", "EditOrder"}
)

# Prefiksy błędów Krakena, które warto ponowić (przeciążenie, limity, chwilowa awaria).
_TRANSIENT_ERRORS: tuple[str, ...] = (
    "EService:Unavailable",
    "EService:Busy",
    "EService:Deadline elapsed",
    "EAPI:Rate limit exceeded",
    "EGeneral:Internal error",
)


class KrakenError(PermanentError):
    """Kraken odrzucił zapytanie. Ponawianie nie pomoże (zły klucz, brak uprawnień,
    blokada IP, zły nonce, błędne argumenty). Wymaga uwagi człowieka."""

    def __init__(self, method: str, errors: list[str]) -> None:
        self.method = method
        self.errors = errors
        super().__init__(f"{method}: {', '.join(errors)}")


class KrakenTransientError(TransientError):
    """Chwilowy problem po stronie Krakena lub sieci. Można ponowić."""


class ForbiddenMethodError(PermanentError):
    """Próba wywołania metody zablokowanej w kodzie. Zapytanie nie zostało wysłane."""


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes


class Transport(Protocol):
    def __call__(
        self, method: str, url: str, headers: Mapping[str, str], body: bytes | None, timeout: float
    ) -> HttpResponse: ...


def _ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()  # weryfikacja certyfikatu i nazwy hosta
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def urllib_transport(
    method: str, url: str, headers: Mapping[str, str], body: bytes | None, timeout: float
) -> HttpResponse:
    if not url.startswith("https://"):
        raise PermanentError(f"Odmowa połączenia bez HTTPS: {url}")
    req = urllib.request.Request(url, data=body, headers=dict(headers), method=method)  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:  # noqa: S310
            return HttpResponse(resp.status, resp.read())
    except urllib.error.HTTPError as exc:
        return HttpResponse(exc.code, exc.read() or b"")
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, ssl.SSLError):
            raise PermanentError(f"Błąd TLS/certyfikatu: {reason}") from exc
        raise KrakenTransientError(f"Błąd sieci: {reason}") from exc


def sign(url_path: str, post_data: str, nonce: str, secret_b64: str) -> str:
    """API-Sign = base64(HMAC-SHA512(base64dec(secret), path + SHA256(nonce + postdata)))."""
    sha = hashlib.sha256((nonce + post_data).encode()).digest()
    mac = hmac.new(base64.b64decode(secret_b64), url_path.encode() + sha, hashlib.sha512)
    return base64.b64encode(mac.digest()).decode()


class NonceGenerator:
    """Nonce w milisekundach, zawsze rosnący, także przy szybkich wywołaniach."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._last = 0
        self._lock = threading.Lock()

    def __call__(self) -> str:
        with self._lock:
            self._last = max(self._last + 1, int(self._clock() * 1000))
            return str(self._last)


class KrakenClient:
    def __init__(
        self,
        cfg: KrakenConfig,
        retry_cfg: RetryConfig,
        *,
        api_key: str | None = None,
        api_secret: str | None = None,
        trading_enabled: bool = False,
        transport: Transport = urllib_transport,
        nonce: NonceGenerator | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if (api_key is None) != (api_secret is None):
            raise ValueError("Podaj oba: api_key i api_secret, albo żadnego")
        if api_secret is not None:
            try:
                base64.b64decode(api_secret, validate=True)
            except ValueError as exc:
                raise ValueError("api_secret nie jest poprawnym base64 (zły klucz?)") from exc
        self._cfg = cfg
        self._key = api_key
        self._secret = api_secret
        self._trading = trading_enabled
        self._transport = transport
        self._nonce = nonce or NonceGenerator()
        self._retry = retry(retry_cfg, sleep=sleep)

    # --- warstwa niskopoziomowa -------------------------------------------------

    def public(self, method: str, params: Mapping[str, Any] | None = None) -> Any:
        path = f"/{API_VERSION}/public/{method}"
        query = urllib.parse.urlencode(params or {})
        url = self._cfg.base_url + path + (f"?{query}" if query else "")

        @self._retry
        def call() -> Any:
            resp = self._transport("GET", url, {"User-Agent": "krakenbot"}, None,
                                   self._cfg.timeout_s)
            return self._parse(method, resp)

        return call()

    def private(self, method: str, params: Mapping[str, Any] | None = None) -> Any:
        self._check_allowed(method)
        if self._key is None or self._secret is None:
            raise PermanentError(f"{method}: brak klucza API Kraken")
        path = f"/{API_VERSION}/private/{method}"
        url = self._cfg.base_url + path

        @self._retry
        def call() -> Any:
            nonce = self._nonce()  # nowy nonce przy każdej próbie
            data = urllib.parse.urlencode({"nonce": nonce, **(params or {})})
            headers = {
                "API-Key": self._key or "",
                "API-Sign": sign(path, data, nonce, self._secret or ""),
                "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
                "User-Agent": "krakenbot",
            }
            resp = self._transport("POST", url, headers, data.encode(), self._cfg.timeout_s)
            return self._parse(method, resp)

        return call()

    def _check_allowed(self, method: str) -> None:
        if method in FORBIDDEN_PRIVATE_METHODS:
            raise ForbiddenMethodError(f"{method}: metoda zablokowana w kodzie")
        if method in READ_ONLY_PRIVATE_METHODS:
            return
        if method in TRADE_PRIVATE_METHODS and self._trading:
            return
        raise ForbiddenMethodError(
            f"{method}: niedozwolona dla tego klienta "
            f"({'handlowy' if self._trading else 'tylko odczyt'})"
        )

    @staticmethod
    def _parse(method: str, resp: HttpResponse) -> Any:
        if resp.status >= 500 or resp.status == 429:
            raise KrakenTransientError(f"{method}: HTTP {resp.status}")
        try:
            payload = json.loads(resp.body)
        except ValueError as exc:
            if resp.status >= 400:
                raise KrakenError(method, [f"HTTP {resp.status}"]) from exc
            raise KrakenTransientError(f"{method}: odpowiedź nie jest JSON-em") from exc
        errors = [e for e in payload.get("error", []) if not e.startswith("W")]  # W = ostrzeżenia
        if errors:
            if all(e.startswith(_TRANSIENT_ERRORS) for e in errors):
                raise KrakenTransientError(f"{method}: {', '.join(errors)}")
            raise KrakenError(method, errors)
        if resp.status >= 400:
            raise KrakenError(method, [f"HTTP {resp.status}"])
        if "result" not in payload:
            raise KrakenTransientError(f"{method}: brak pola result")
        return payload["result"]

    # --- metody publiczne ----------------------------------------------------------

    def server_time(self) -> int:
        return int(self.public("Time")["unixtime"])

    def system_status(self) -> str:
        return str(self.public("SystemStatus")["status"])

    def asset_pairs(self) -> dict[str, dict[str, Any]]:
        return self.public("AssetPairs")

    def ticker(self, pairs: list[str]) -> dict[str, dict[str, Any]]:
        if not pairs:
            return {}
        out: dict[str, dict[str, Any]] = {}
        for i in range(0, len(pairs), 50):  # porcje, żeby nie przekroczyć długości URL
            out.update(self.public("Ticker", {"pair": ",".join(pairs[i : i + 50])}))
        return out

    # --- metody prywatne (odczyt) -----------------------------------------------------

    def balance(self) -> dict[str, str]:
        return self.private("Balance")

    def ledgers(self, **params: Any) -> dict[str, Any]:
        return self.private("Ledgers", params)
