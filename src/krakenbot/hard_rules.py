"""Twarde reguły zapisane w kodzie.

Tych wartości nie zmienia ani plik konfiguracyjny, ani Telegram, ani agenci.
Zmiana wymaga zmiany kodu, przeglądu i nowego wdrożenia.
"""

from __future__ import annotations

# Rynek
MARKET = "spot"
QUOTE_CURRENCY = "EUR"
LEVERAGE_ALLOWED = False

# Dozwolone typy zleceń Kraken: tylko po cenie rynkowej oraz warianty rynkowe SL/TP.
# Brak "limit", "stop-loss-limit", "take-profit-limit" itd. (bez ofert w orderbooku).
ALLOWED_ORDER_TYPES: frozenset[str] = frozenset({"market", "stop-loss", "take-profit"})

# Stablecoiny są wykluczone z handlu (lista uzupełniana w Etapie 5 na realnych danych).
EXCLUDED_BASE_ASSETS: frozenset[str] = frozenset(
    {"USDT", "USDC", "DAI", "PYUSD", "EURT", "EURC", "USDG", "TUSD", "FDUSD", "RLUSD", "USD", "GBP"}
)

# Absolutne granice parametrów ryzyka. Granice w konfiguracji (risk_bounds) muszą się
# w nich mieścić, a wartości w risk muszą mieścić się w risk_bounds.
# Klucz -> (minimum, maksimum)
ABSOLUTE_RISK_LIMITS: dict[str, tuple[float, float]] = {
    "max_position_pct": (1.0, 100.0),
    "max_open_positions": (1, 30),
    "cash_reserve_pct": (0.0, 90.0),
    "max_loss_per_trade_pct": (0.1, 10.0),
    "min_hold_hours": (0, 24 * 30),
    "max_new_trades_per_day": (0, 10),
    "max_new_trades_per_week": (0, 30),
    "min_expected_move_pct": (0.5, 50.0),
    "daily_loss_limit_pct": (1.0, 50.0),
}
