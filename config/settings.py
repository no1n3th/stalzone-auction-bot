"""Central application settings (pydantic-settings, env-driven)."""

from __future__ import annotations

import json
import logging
from functools import cached_property, lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_BASE_API_URL = "https://eapi.stalcraft.net"
DEMO_BASE_API_URL = "https://dapi.stalcraft.net"
EXBO_TOKEN_URL = "https://exbo.net/oauth/token"

QUALITY_NAMES: dict[int, str] = {
    0: "Обычная",
    1: "Необычная",
    2: "Редкая",
    3: "Особая",
    4: "Исключительная",
    5: "Легендарная",
}

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """All knobs are env-configurable; defaults are production-safe."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Stalcraft API ---
    base_api_url: str = DEFAULT_BASE_API_URL
    region: str = "RU"  # RU | EU | NA | SEA
    client_id: str = ""
    client_secret: SecretStr = SecretStr("")  # AUDIT S8
    exbo_url: str = EXBO_TOKEN_URL
    api_timeout_sec: float = 15.0
    api_max_retries: int = 3
    # Пауза после отказа OAuth (неверные CLIENT_ID/CLIENT_SECRET), чтобы не долбить exbo
    auth_backoff_sec: float = 300.0
    scan_limit: int = 200  # API hard cap

    # --- Rate limiting ---
    rate_limit_capacity: int = 400
    rate_limit_window_sec: int = 60

    # --- Scanner ---
    liquid_interval_sec: int = 60
    illiquid_interval_multiplier: float = 10.0
    illiquid_default_weight: float = 6.67  # x6.67 when no explicit threshold
    scan_batch_size: int = 8
    scan_concurrency: int = 4  # parallel item scans per batch
    fee_pct: float = 5.0  # auction fee, %
    default_profit_threshold_pct: float = 10.0
    liquid_earnings_by_quality_json: str = '{"0": 10, "1": 12, "2": 16, "3": 14, "4": 18, "5": 23}'
    min_sample_size: int = 5
    # AUDIT D3: "legacy" = fee counted twice in the threshold (old behaviour);
    # "net" = threshold is pure net profit after the single 5% fee (default).
    threshold_mode: Literal["net", "gross"] = "net"
    # AUDIT S1: who may register. "bootstrap" = only while the user table is
    # empty (first account becomes admin, then registration closes); "open" =
    # anyone; "invite" = reserved (rejected until implemented).
    registration_mode: Literal["bootstrap", "open", "invite"] = "bootstrap"
    # mid-upgrade multipliers
    mid_low_multiplier: float = 1.01  # +2..+9  vs P_ref(0)
    mid_high_multiplier: float = 0.80  # +10..+11 vs P_ref(15)
    mid_top_multiplier: float = 0.90  # +12..+14 vs P_ref(15)

    # --- History / ring buffer ---
    history_rows_per_item: int = 50
    history_cheapest_only: bool = False
    history_retention_days: int = 90
    history_sync_interval_sec: float = 900

    # --- Meta analytics ---
    dump_drop_pct: float = 20.0
    dump_window_hours: int = 24
    meta_exit_min_lots: int = 10
    meta_exit_below_avg_pct: float = 25.0
    stabilization_hours: int = 48
    stabilization_max_spread_pct: float = 15.0
    meta_enter_window_hours: int = 6
    meta_enter_surge_pct: float = 50.0
    fast_adapt_weight: float = 3.0
    meta_check_interval_sec: float = 600

    # --- Seasonality ---
    season_prewarn_days: int = 7
    seasonal_history_window_days: int = 14
    season_dip_buy_pct: float = 30.0
    season_check_interval_sec: float = 3600

    # --- Notifications ---
    discord_webhook_url: SecretStr = SecretStr("")  # AUDIT S8
    discord_extra_webhooks_json: str = "[]"
    notify_cooldown_min: int = 30
    notify_batch_size: int = 10
    notify_send_interval_sec: float = 2.0
    discord_rate_per_sec: int = 5  # Discord token bucket, req/s
    chart_enabled: bool = True
    chart_dir: str = "data/charts"
    chart_cache_max_files: int = 300
    chart_render_workers: int = 2  # concurrent chart renders
    chart_queue_size: int = 64

    # --- Database ---
    database_url: str = "sqlite+aiosqlite:///./data/stalzone.db"

    # --- Web ---
    web_host: str = "0.0.0.0"
    web_port: int = 8080
    session_ttl_hours: int = 168
    admin_usernames: str = "admin"
    cookie_secure: bool = False  # True behind HTTPS: cookie is never sent over plain HTTP
    ws_feed_queue_size: int = 200

    # --- System / ops ---
    log_level: str = "INFO"
    log_json: bool = True
    oom_watchdog_mb: int = 50
    oom_check_interval_sec: float = 30
    metrics_enabled: bool = True
    # P2: non-empty -> /metrics answers 403 without ?token=... (see web/app.py)
    metrics_token: str = ""
    # P2: trusted proxies for X-Forwarded-For (uvicorn proxy_headers), so
    # throttles key on the real client IP instead of the proxy's address.
    forwarded_allow_ips: str = "127.0.0.1"
    items_db_refresh_hours: int = 24
    items_db_path: str = "data/items_db.json"
    artifacts_config_path: str = "config/artifacts.yaml"
    seasons_config_path: str = "config/seasons.yaml"
    config_reload_interval_sec: float = 30
    alerting_webhook_url: SecretStr = SecretStr(
        ""
    )  # AUDIT S8  # ops alerts (401 token invalidation etc.)
    alert_cooldown_sec: float = 300.0  # min gap between repeated alerts of one kind

    @field_validator("region")
    @classmethod
    def _region_upper(cls, v: str) -> str:
        v = v.upper()
        if v not in {"RU", "EU", "NA", "SEA"}:
            raise ValueError(f"unsupported region: {v}")
        return v

    @property
    def fee(self) -> float:
        return self.fee_pct / 100.0

    @property
    def profit_threshold_pct(self) -> float:
        """Backward-compatible alias (tests + older call sites)."""
        return self.default_profit_threshold_pct

    @cached_property
    def liquid_earnings_by_quality(self) -> dict[int, float]:
        # AUDIT Q3: broken JSON must not silently change pricing — warn and
        # fall back to the documented defaults.
        try:
            raw = json.loads(self.liquid_earnings_by_quality_json)
            return {int(k): float(v) for k, v in raw.items()}
        except (TypeError, ValueError, json.JSONDecodeError):
            logging.getLogger(__name__).warning("settings_liquid_earnings_json_invalid")
            return {0: 10.0, 1: 12.0, 2: 16.0, 3: 14.0, 4: 18.0, 5: 23.0}

    @cached_property
    def discord_webhooks(self) -> list[str]:
        hooks: list[str] = []
        if self.discord_webhook_url.get_secret_value():
            hooks.append(self.discord_webhook_url.get_secret_value())
        try:
            extra = json.loads(self.discord_extra_webhooks_json)
            hooks.extend(str(h) for h in extra if h)
        except json.JSONDecodeError:
            # P1-13: a malformed env value used to vanish without a trace
            logging.getLogger(__name__).warning(
                "DISCORD_EXTRA_WEBHOOKS_JSON is not valid JSON — extra webhooks ignored"
            )
        return hooks

    @property
    def admin_list(self) -> list[str]:
        return [u.strip() for u in self.admin_usernames.split(",") if u.strip()]

    def resolve_path(self, p: str) -> Path:
        path = Path(p)
        return path if path.is_absolute() else BASE_DIR / path


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reveal_secret(value: object) -> str:
    """AUDIT S8: unwrap SecretStr; tolerate plain strings (test fakes)."""
    getter = getattr(value, "get_secret_value", None)
    return str(getter()) if callable(getter) else str(value or "")
