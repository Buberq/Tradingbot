"""Wycena portfela w EUR na podstawie sald i cen z Krakena.

Kraken zwraca salda pod własnymi kodami aktywów (np. ZEUR, XXBT, SOL). Saldo bez
sufiksu to środki na rynku SPOT. Salda z sufiksem (np. "DOT.S", "ETH.F", "EUR.HOLD")
to środki w stakingu, Earn albo zablokowane: bot ich nie handluje, więc nie wchodzą
do wartości portfela, ale raportujemy je osobno, żeby nic nie znikało z widoku.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from krakenbot.db import utcnow

EUR_ASSET = "ZEUR"
_DUST = Decimal("0")


@dataclass(frozen=True)
class Position:
    asset: str
    pair: str
    volume: Decimal
    price_eur: Decimal
    value_eur: Decimal


@dataclass(frozen=True)
class Valuation:
    cash_eur: Decimal
    positions: dict[str, Position]
    unpriced: dict[str, Decimal] = field(default_factory=dict)  # brak pary do EUR
    non_spot: dict[str, Decimal] = field(default_factory=dict)  # staking, Earn, HOLD

    @property
    def total_eur(self) -> Decimal:
        return self.cash_eur + sum((p.value_eur for p in self.positions.values()), Decimal(0))

    def positions_json(self) -> dict[str, Any]:
        return {
            a: {
                "pair": p.pair,
                "volume": str(p.volume),
                "price_eur": str(p.price_eur),
                "value_eur": str(p.value_eur.quantize(Decimal("0.01"))),
            }
            for a, p in sorted(self.positions.items())
        }


def eur_pairs(asset_pairs: dict[str, dict[str, Any]]) -> dict[str, str]:
    """Mapa: kod aktywa bazowego -> klucz pary do EUR (np. XXBT -> XXBTZEUR)."""
    out: dict[str, str] = {}
    for key, info in asset_pairs.items():
        if info.get("quote") != EUR_ASSET or key.endswith(".d"):  # .d = pary "dark pool"
            continue
        if info.get("status", "online") not in ("online", "cancel_only", "post_only",
                                                 "limit_only", "reduce_only"):
            continue
        base = info.get("base")
        if base and base not in out:
            out[base] = key
    return out


def split_balances(balances: dict[str, str]) -> tuple[dict[str, Decimal], dict[str, Decimal]]:
    """Dzieli salda na SPOT i pozostałe (z sufiksem po kropce). Pomija zerowe."""
    spot: dict[str, Decimal] = {}
    other: dict[str, Decimal] = {}
    for asset, amount in balances.items():
        value = Decimal(amount)
        if value <= _DUST:
            continue
        (other if "." in asset else spot)[asset] = value
    return spot, other


def value_portfolio(
    balances: dict[str, str],
    asset_pairs: dict[str, dict[str, Any]],
    tickers: dict[str, dict[str, Any]],
) -> Valuation:
    spot, non_spot = split_balances(balances)
    pairs = eur_pairs(asset_pairs)
    cash = spot.pop(EUR_ASSET, Decimal(0))

    positions: dict[str, Position] = {}
    unpriced: dict[str, Decimal] = {}
    for asset, volume in spot.items():
        pair = pairs.get(asset)
        tick = tickers.get(pair) if pair else None
        if not tick:
            unpriced[asset] = volume
            continue
        price = Decimal(tick["c"][0])  # c = ostatnia transakcja [cena, wolumen]
        positions[asset] = Position(asset, pair, volume, price, volume * price)
    return Valuation(cash, positions, unpriced, non_spot)


def pairs_to_price(balances: dict[str, str], asset_pairs: dict[str, dict[str, Any]]) -> list[str]:
    spot, _ = split_balances(balances)
    pairs = eur_pairs(asset_pairs)
    return sorted({pairs[a] for a in spot if a != EUR_ASSET and a in pairs})


def save_snapshot(conn: sqlite3.Connection, *, mode: str, valuation: Valuation, source: str) -> int:
    details = {
        "positions": valuation.positions_json(),
        "unpriced": {k: str(v) for k, v in valuation.unpriced.items()},
        "non_spot": {k: str(v) for k, v in valuation.non_spot.items()},
    }
    cur = conn.execute(
        "INSERT INTO portfolio_snapshots (ts, mode, total_eur, cash_eur, positions, source)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (
            utcnow(),
            mode,
            float(valuation.total_eur),
            float(valuation.cash_eur),
            json.dumps(details, ensure_ascii=False),
            source,
        ),
    )
    return int(cur.lastrowid)


def format_valuation(v: Valuation) -> str:
    lines = [f"Wartość portfela: {v.total_eur:,.2f} EUR", f"  EUR (gotówka): {v.cash_eur:,.2f}"]
    for a, p in sorted(v.positions.items(), key=lambda kv: -kv[1].value_eur):
        share = p.value_eur / v.total_eur * 100 if v.total_eur else Decimal(0)
        lines.append(
            f"  {a}: {p.volume} × {p.price_eur} = {p.value_eur:,.2f} EUR ({share:.1f}%)"
        )
    if v.unpriced:
        lines.append("Bez pary do EUR (niewycenione): "
                     + ", ".join(f"{a} {q}" for a, q in v.unpriced.items()))
    if v.non_spot:
        lines.append("Poza SPOT (staking/Earn/zablokowane, nie liczone): "
                     + ", ".join(f"{a} {q}" for a, q in v.non_spot.items()))
    return "\n".join(lines)
