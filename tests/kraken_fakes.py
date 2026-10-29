"""Fałszywy transport HTTP i przykładowe odpowiedzi w formacie API Kraken."""

from __future__ import annotations

import json
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from krakenbot.kraken_client import HttpResponse

# Sekret z oficjalnego przykładu w dokumentacji Krakena (nie jest prawdziwym kluczem).
DOC_SECRET = (
    "kQH5HW/8p1uGOVjbgWA7FunAmGO8lsSUXNsu3eow76sz84Q18fWxnyRzBHCd3pd5nE9qa99HAZtuZuj6F1huXg=="
)

ASSET_PAIRS = {
    "XXBTZEUR": {"altname": "XBTEUR", "wsname": "XBT/EUR", "base": "XXBT", "quote": "ZEUR",
                 "ordermin": "0.0001", "status": "online"},
    "XETHZEUR": {"altname": "ETHEUR", "wsname": "ETH/EUR", "base": "XETH", "quote": "ZEUR",
                 "ordermin": "0.002", "status": "online"},
    "SOLEUR": {"altname": "SOLEUR", "wsname": "SOL/EUR", "base": "SOL", "quote": "ZEUR",
               "ordermin": "0.02", "status": "online"},
    "XXBTZUSD": {"altname": "XBTUSD", "wsname": "XBT/USD", "base": "XXBT", "quote": "ZUSD",
                 "ordermin": "0.0001", "status": "online"},
    "XXBTZEUR.d": {"altname": "XBTEUR.d", "base": "XXBT", "quote": "ZEUR", "status": "online"},
    "DEADEUR": {"altname": "DEADEUR", "base": "DEAD", "quote": "ZEUR", "status": "delisted"},
}

TICKERS = {
    "XXBTZEUR": {"c": ["60000.0", "0.01"], "a": ["60001", "1", "1.0"], "b": ["59999", "1", "1.0"]},
    "XETHZEUR": {"c": ["2500.5", "0.5"]},
    "SOLEUR": {"c": ["150.00", "3"]},
}

BALANCES = {
    "ZEUR": "120.5000",
    "XXBT": "0.0010000000",
    "XETH": "0.0200000000",
    "SOL": "0.0000000000",
    "DOT.S": "10.0",
    "ZUSD": "5.00",
}


def ok(result: Any) -> HttpResponse:
    return HttpResponse(200, json.dumps({"error": [], "result": result}).encode())


def err(*errors: str, status: int = 200) -> HttpResponse:
    return HttpResponse(status, json.dumps({"error": list(errors)}).encode())


@dataclass
class Call:
    method: str
    url: str
    headers: dict[str, str]
    body: bytes | None

    @property
    def path(self) -> str:
        return urllib.parse.urlsplit(self.url).path

    @property
    def form(self) -> dict[str, str]:
        return dict(urllib.parse.parse_qsl((self.body or b"").decode()))

    @property
    def query(self) -> dict[str, str]:
        return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.url).query))


@dataclass
class FakeTransport:
    """Odpowiada według ścieżki URL. Wartość może być listą: kolejne odpowiedzi po kolei."""

    routes: dict[str, Any] = field(default_factory=dict)
    calls: list[Call] = field(default_factory=list)

    def __call__(
        self, method: str, url: str, headers: Mapping[str, str], body: bytes | None, timeout: float
    ) -> HttpResponse:
        call = Call(method, url, dict(headers), body)
        self.calls.append(call)
        route = self.routes[call.path]
        if isinstance(route, list):
            route = route.pop(0) if len(route) > 1 else route[0]
        if isinstance(route, BaseException):
            raise route
        return route


def kraken_routes(**overrides: Any) -> dict[str, Any]:
    routes: dict[str, Any] = {
        "/0/public/Time": ok({"unixtime": 1761736000, "rfc1123": "x"}),
        "/0/public/SystemStatus": ok({"status": "online", "timestamp": "x"}),
        "/0/public/AssetPairs": ok(ASSET_PAIRS),
        "/0/public/Ticker": ok(TICKERS),
        "/0/private/Balance": ok(BALANCES),
    }
    routes.update(overrides)
    return routes
