"""Ładowanie i walidacja konfiguracji.

Źródła, w kolejności:
1. plik YAML (domyślnie config/config.yaml, ścieżka z KRAKENBOT_CONFIG),
2. wybrane nadpisania ze zmiennych środowiskowych (KRAKENBOT_*),
3. sekrety wyłącznie ze zmiennych środowiskowych (nigdy z YAML).

Tryb live wymaga dwóch świadomych kroków: `mode: live` w pliku ORAZ zmiennej
KRAKENBOT_ALLOW_LIVE=yes. Samo jedno z nich nie wystarcza i bot odmawia startu.
Potwierdzenie na Telegramie dojdzie w Etapie 3.
"""

from __future__ import annotations

import os
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from krakenbot.hard_rules import ABSOLUTE_RISK_LIMITS

DEFAULT_CONFIG_PATH = Path("config/config.yaml")
ALLOW_LIVE_ENV = "KRAKENBOT_ALLOW_LIVE"

# Klucze sekretów: nazwa pola -> zmienna środowiskowa
SECRET_ENV_VARS: dict[str, str] = {
    "kraken_read_key": "KRAKEN_READ_API_KEY",
    "kraken_read_secret": "KRAKEN_READ_API_SECRET",
    "kraken_trade_key": "KRAKEN_TRADE_API_KEY",
    "kraken_trade_secret": "KRAKEN_TRADE_API_SECRET",
    "anthropic_api_key": "ANTHROPIC_API_KEY",
    "telegram_bot_token": "TELEGRAM_BOT_TOKEN",
    "coingecko_api_key": "COINGECKO_API_KEY",
}


class ConfigError(Exception):
    """Błędna lub niebezpieczna konfiguracja. Bot nie startuje."""


class Mode(StrEnum):
    PAPER = "paper"
    LIVE = "live"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PathsConfig(_Strict):
    db_path: Path = Path("data/krakenbot.sqlite3")
    log_dir: Path = Path("logs")


class LoggingConfig(_Strict):
    level: str = "INFO"
    max_bytes: int = Field(default=5_000_000, ge=10_000)
    backup_count: int = Field(default=10, ge=1, le=100)


class RetryConfig(_Strict):
    max_attempts: int = Field(default=5, ge=1, le=20)
    base_delay_s: float = Field(default=1.0, gt=0, le=60)
    max_delay_s: float = Field(default=60.0, gt=0, le=600)
    jitter: bool = True


class KrakenConfig(_Strict):
    base_url: str = "https://api.kraken.com"
    timeout_s: float = Field(default=15.0, gt=0, le=120)

    @model_validator(mode="after")
    def _https_only(self) -> KrakenConfig:
        if not self.base_url.startswith("https://"):
            raise ValueError("kraken.base_url musi używać HTTPS")
        return self


class ScheduleConfig(_Strict):
    review_interval_minutes: int = Field(default=60, ge=15, le=24 * 60)
    daily_decision_hour_utc: int = Field(default=7, ge=0, le=23)


class RiskConfig(_Strict):
    """Parametry ryzyka. Wartości procentowe liczone od wartości portfela."""

    max_position_pct: float = 50.0
    max_open_positions: int = 10
    cash_reserve_pct: float = 10.0
    max_loss_per_trade_pct: float = 2.0
    min_hold_hours: int = 48
    max_new_trades_per_day: int = 1
    max_new_trades_per_week: int = 4
    min_expected_move_pct: float = 3.0
    daily_loss_limit_pct: float = 20.0


class Bound(_Strict):
    min: float
    max: float

    @model_validator(mode="after")
    def _ordered(self) -> Bound:
        if self.min > self.max:
            raise ValueError(f"min ({self.min}) > max ({self.max})")
        return self


def _default_bounds() -> dict[str, Bound]:
    return {k: Bound(min=lo, max=hi) for k, (lo, hi) in ABSOLUTE_RISK_LIMITS.items()}


class Secrets(_Strict):
    """Sekrety wyłącznie ze zmiennych środowiskowych. repr nie pokazuje wartości."""

    kraken_read_key: SecretStr | None = None
    kraken_read_secret: SecretStr | None = None
    kraken_trade_key: SecretStr | None = None
    kraken_trade_secret: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None
    telegram_bot_token: SecretStr | None = None
    coingecko_api_key: SecretStr | None = None

    def missing(self, *names: str) -> list[str]:
        """Zwraca nazwy zmiennych środowiskowych, których brakuje dla podanych pól."""
        return [SECRET_ENV_VARS[n] for n in names if getattr(self, n) is None]

    def present_values(self) -> list[str]:
        return [v.get_secret_value() for v in self.__dict__.values() if v is not None]


class Settings(_Strict):
    mode: Mode = Mode.PAPER
    paths: PathsConfig = PathsConfig()
    logging: LoggingConfig = LoggingConfig()
    retry: RetryConfig = RetryConfig()
    kraken: KrakenConfig = KrakenConfig()
    schedule: ScheduleConfig = ScheduleConfig()
    risk: RiskConfig = RiskConfig()
    risk_bounds: dict[str, Bound] = Field(default_factory=_default_bounds)
    telegram_chat_id: int | None = None
    secrets: Secrets = Secrets()

    @model_validator(mode="after")
    def _check_risk(self) -> Settings:
        unknown = set(self.risk_bounds) - set(ABSOLUTE_RISK_LIMITS)
        if unknown:
            raise ValueError(f"risk_bounds: nieznane klucze {sorted(unknown)}")
        bounds = {**_default_bounds(), **self.risk_bounds}
        for key, (abs_lo, abs_hi) in ABSOLUTE_RISK_LIMITS.items():
            b = bounds[key]
            if b.min < abs_lo or b.max > abs_hi:
                raise ValueError(
                    f"risk_bounds.{key} [{b.min}, {b.max}] wychodzi poza twarde granice "
                    f"kodu [{abs_lo}, {abs_hi}]"
                )
            value = getattr(self.risk, key)
            if not (b.min <= value <= b.max):
                raise ValueError(f"risk.{key}={value} poza granicami [{b.min}, {b.max}]")
        object.__setattr__(self, "risk_bounds", bounds)
        return self


def _env_overrides(env: dict[str, str]) -> dict[str, Any]:
    """Nadpisania ze środowiska. Celowo NIE ma tu trybu (mode) ani parametrów ryzyka."""
    out: dict[str, Any] = {}
    if v := env.get("KRAKENBOT_DB_PATH"):
        out.setdefault("paths", {})["db_path"] = v
    if v := env.get("KRAKENBOT_LOG_DIR"):
        out.setdefault("paths", {})["log_dir"] = v
    if v := env.get("KRAKENBOT_LOG_LEVEL"):
        out.setdefault("logging", {})["level"] = v
    if v := env.get("TELEGRAM_CHAT_ID"):
        out["telegram_chat_id"] = v
    return out


def _deep_merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k] = _deep_merge(merged[k], v)
        else:
            merged[k] = v
    return merged


def load_settings(
    path: Path | str | None = None, env: dict[str, str] | None = None
) -> Settings:
    env = dict(os.environ) if env is None else env
    path = Path(path or env.get("KRAKENBOT_CONFIG") or DEFAULT_CONFIG_PATH)
    if not path.is_file():
        raise ConfigError(
            f"Brak pliku konfiguracji: {path}. Skopiuj config/config.example.yaml "
            "do config/config.yaml."
        )

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: oczekiwano mapy YAML na najwyższym poziomie")
    if "secrets" in raw:
        raise ConfigError(
            f"{path}: sekrety nie mogą być w pliku YAML. Ustaw je w zmiennych środowiskowych."
        )

    data = _deep_merge(raw, _env_overrides(env))
    data["secrets"] = {
        field: env[var] for field, var in SECRET_ENV_VARS.items() if env.get(var)
    }

    try:
        settings = Settings.model_validate(data)
    except ValueError as exc:  # pydantic.ValidationError dziedziczy po ValueError
        raise ConfigError(f"Błędna konfiguracja w {path}:\n{exc}") from exc

    if settings.mode is Mode.LIVE and env.get(ALLOW_LIVE_ENV) != "yes":
        raise ConfigError(
            f"Plik ustawia mode: live, ale brak {ALLOW_LIVE_ENV}=yes. "
            "Tryb live wymaga obu świadomych zmian. Bot nie wystartuje."
        )
    return settings
