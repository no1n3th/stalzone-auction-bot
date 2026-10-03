"""Async Stalcraft API client: OAuth2 client-credentials, retries, rate-limit sync."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import aiohttp
import structlog

from config.settings import Settings, reveal_secret
from utils.circuit_breaker import CircuitBreaker
from utils.rate_limiter import TokenBucket

log = structlog.get_logger(__name__)

AlertHook = Callable[[str, str], Awaitable[None]]

ITEMS_DB_REPO_API = (
    "https://api.github.com/repositories/589389462"  # EXBO-Studio/stalcraft-database
)
ITEMS_DB_RAW = (
    "https://raw.githubusercontent.com/EXBO-Studio/stalcraft-database/main/ru/listing.json"
)


class ItemRequestError(Exception):
    """AUDIT D5: permanent 4xx on an item request — the caller should
    quarantine the item, not retry it and not feed the circuit breaker."""


class TokenInvalidError(Exception):
    pass


class CircuitOpenError(RuntimeError):
    """P1-12: distinguish "breaker open" from generic request failures."""


class StalcraftClient:
    def __init__(
        self,
        settings: Settings,
        session: aiohttp.ClientSession,
        rate_limiter: TokenBucket | None = None,
        breaker: CircuitBreaker | None = None,
        on_alert: AlertHook | None = None,
    ) -> None:
        self.settings = settings
        self.session = session
        self.rate_limiter = rate_limiter
        self.breaker = breaker
        self.on_alert = on_alert
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        self._token_lock = asyncio.Lock()
        self._auth_backoff_until: float = 0.0

    async def _alert(self, kind: str, text: str) -> None:
        if self.on_alert is not None:
            with contextlib.suppress(Exception):
                await self.on_alert(kind, text)

    # ---------- auth ----------
    async def _fetch_token(self) -> str:
        auth = base64.b64encode(
            f"{self.settings.client_id}:{reveal_secret(self.settings.client_secret)}".encode()
        ).decode()
        async with self.session.post(
            self.settings.exbo_url,
            # scope не отправляем: приложения Stalzone API выдаются без
            # scope-ов (JWT: "scopes":[]) — запрос с scope=read отклоняется
            # exbo с invalid_scope. Доступ регулируется на уровне приложения.
            data={"grant_type": "client_credentials"},
            headers={
                "Authorization": f"Basic {auth}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        ) as resp:
            if resp.status in (400, 401):
                raise TokenInvalidError(f"oauth token rejected: {resp.status}")
            resp.raise_for_status()
            data = await resp.json()
        token = str(data["access_token"])
        expires_in = float(data.get("expires_in", 3600))
        self._token = token
        self._token_expires_at = time.monotonic() + max(60.0, expires_in - 60.0)
        log.info("oauth_token_refreshed", expires_in=expires_in)
        return token

    async def token(self) -> str:
        async with self._token_lock:
            now = time.monotonic()
            if now < self._auth_backoff_until:
                raise TokenInvalidError(
                    "oauth auth backoff — check CLIENT_ID/CLIENT_SECRET in .env"
                )
            if self._token is None or now >= self._token_expires_at:
                try:
                    return await self._fetch_token()
                except TokenInvalidError:
                    # Credentials rejected: stop hammering exbo and the API,
                    # alert once, retry only after the backoff window.
                    self._auth_backoff_until = now + self.settings.auth_backoff_sec
                    log.error(
                        "oauth_credentials_rejected",
                        backoff_sec=self.settings.auth_backoff_sec,
                        hint="проверьте CLIENT_ID/CLIENT_SECRET в .env",
                    )
                    await self._alert(
                        "oauth",
                        "OAuth 401/400 от exbo — проверьте CLIENT_ID/CLIENT_SECRET, "
                        f"пауза {self.settings.auth_backoff_sec:.0f}s",
                    )
                    raise
            return self._token

    async def invalidate_token(self) -> None:
        async with self._token_lock:
            self._token = None
            self._token_expires_at = 0.0

    # ---------- low-level GET ----------
    async def _get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.settings.base_api_url}/{self.settings.region}/{path.lstrip('/')}"
        retries = self.settings.api_max_retries
        for attempt in range(retries + 1):
            if self.breaker is not None and not self.breaker.allow():
                raise CircuitOpenError("circuit breaker open")
            if self.rate_limiter is not None:
                await self.rate_limiter.acquire()
            token = await self.token()
            try:
                async with self.session.get(
                    url,
                    params=params,
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=aiohttp.ClientTimeout(total=self.settings.api_timeout_sec),
                ) as resp:
                    if self.rate_limiter is not None:
                        await self.rate_limiter.update_from_headers(resp.headers)
                    if resp.status == 401:
                        # token invalidated -> drop it, alert, refetch once
                        await self.invalidate_token()
                        log.error("api_401_token_invalidated", path=path)
                        await self._alert("api_401", f"API ответил 401 на {path} — токен сброшен")
                        if attempt == 0:
                            continue
                        raise TokenInvalidError("401 after token refresh")
                    if resp.status == 429:
                        try:
                            retry_after = float(resp.headers.get("Retry-After", "1"))
                        except ValueError:
                            # AUDIT D5: Retry-After can be an HTTP-date header
                            retry_after = 5.0
                        log.warning("api_429_backoff", retry_after=retry_after)
                        await asyncio.sleep(min(retry_after, 30.0))
                        continue
                    if resp.status >= 500:
                        if attempt < retries:
                            await asyncio.sleep(min(2.0**attempt, 10.0))
                            continue
                        resp.raise_for_status()
                    if resp.status >= 400:
                        # AUDIT D5: a permanent client error (404/400/403) —
                        # raise immediately: no retries (wasted token budget),
                        # no breaker failure (two bad item_ids used to open
                        # the circuit for the entire scanner).
                        raise ItemRequestError(f"{path} -> HTTP {resp.status}")
                    resp.raise_for_status()
                    if self.breaker is not None:
                        self.breaker.on_success()
                    try:
                        return await resp.json()
                    except ValueError as exc:  # garbage body — not retryable
                        raise RuntimeError(f"api returned invalid JSON for {path}") from exc
            except (TimeoutError, aiohttp.ClientError) as exc:
                if self.breaker is not None:
                    self.breaker.on_failure()
                if attempt < retries:
                    await asyncio.sleep(min(2.0**attempt, 10.0))
                    continue
                raise RuntimeError(f"api request failed: {exc}") from exc
        raise RuntimeError("api retries exhausted")

    # ---------- auction ----------
    async def get_lots(self, item_id: str, limit: int = 200) -> list[dict[str, Any]]:
        """GET /{region}/auction/{item_id}/lots (limit <= 200)."""
        limit = max(1, min(limit, 200))
        data = await self._get_json(
            f"auction/{item_id}/lots",
            params={"limit": limit, "sort": "buyout_price", "order": "asc"},
        )
        if isinstance(data, dict):
            lots: list[dict[str, Any]] = data.get("lots", data.get("data")) or []
            return list(lots)
        return list(data or [])

    async def get_history(
        self, item_id: str, limit: int = 200, offset: int = 0
    ) -> list[dict[str, Any]]:
        """GET /{region}/auction/{item_id}/history (paginated — P0-4)."""
        limit = max(1, min(limit, 200))
        params: dict[str, Any] = {"limit": limit}
        if offset:
            params["offset"] = offset
        data = await self._get_json(f"auction/{item_id}/history", params=params)
        if isinstance(data, dict):
            prices: list[dict[str, Any]] = data.get("prices", data.get("data")) or []
            return list(prices)
        return list(data or [])

    # ---------- items db ----------
    async def sync_items_db(self, dest: Path) -> int:
        """Refresh the local items cache from stalcraft-database listing.json."""
        async with self.session.get(ITEMS_DB_RAW) as resp:
            if getattr(resp, "status", 200) >= 400:
                raise RuntimeError(f"items listing fetch failed: HTTP {resp.status}")
            data = await resp.json(content_type=None)
        # P0-1: listing.json is a FLAT list of entries; id = last path segment
        # of `data` (lowercased), name lives in name.lines.ru (fallback en).
        entries = data if isinstance(data, list) else data.get("data", [])
        out: dict[str, dict[str, Any]] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            raw_id = entry.get("data")
            if not raw_id:
                continue
            item_id = os.path.splitext(unquote(str(raw_id)).rstrip("/").rsplit("/", 1)[-1])[0].lower()
            name = entry.get("name")
            lines = name.get("lines") if isinstance(name, dict) else None
            if isinstance(lines, dict):
                name = lines.get("ru") or lines.get("en") or item_id
            else:
                name = item_id
            rec: dict[str, Any] = {"name": str(name or item_id)}
            icon = entry.get("icon")  # P0-1: exact path from the listing itself
            if icon:
                rec["icon"] = str(icon)
            out[item_id] = rec
        if not out:
            # Never overwrite a working cache with an empty parse — the Docker
            # image used to bake "{}" into the volume exactly this way.
            raise RuntimeError("items listing parsed to 0 items — refusing to overwrite cache")
        dest.parent.mkdir(parents=True, exist_ok=True)
        # AUDIT R10: atomic replace — a crash mid-write used to leave a
        # truncated JSON that load_items_db_cache silently swallowed as {}.
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        tmp.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, dest)
        log.info("items_db_synced", count=len(out))
        return len(out)


def load_items_db_cache(path: Path) -> dict[str, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {str(k): str(v.get("name", k)) for k, v in data.items()}
    except (OSError, json.JSONDecodeError, AttributeError):
        return {}


def load_items_icons(path: Path) -> dict[str, str]:
    """item_id -> raw icon path from the cache (P0-1: ru/icons/<subtype>/...)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {
            str(k): str(v["icon"])
            for k, v in data.items()
            if isinstance(v, dict) and v.get("icon")
        }
    except (OSError, json.JSONDecodeError, AttributeError):
        return {}


def get_icon_url(item_id: str) -> str:
    """Deprecated flat-path guess (P0-1 proved the tree is nested). The Discord
    embed uses notifier.icons + icon_url(); this remains for old call sites."""
    return (
        "https://raw.githubusercontent.com/EXBO-Studio/stalcraft-database"
        f"/main/ru/icons/artefact/{item_id}.png"
    )


# ---------- cgroup memory helpers (OOM watchdog) ----------
def cgroup_memory_limit_bytes() -> int | None:
    """cgroup v2 then v1; None when not applicable."""
    for path in (Path("/sys/fs/cgroup/memory.max"),):
        try:
            value = path.read_text().strip()
            if value != "max":
                return int(value)
        except (OSError, ValueError):
            pass
    try:
        return int(Path("/sys/fs/cgroup/memory/memory.limit_in_bytes").read_text().strip())
    except (OSError, ValueError):
        return None


def _inactive_file_bytes(stat_path: Path) -> int:
    """Reclaimable page cache (SQLite WAL, logs) — not real memory pressure."""
    try:
        for line in stat_path.read_text().splitlines():
            key, _, value = line.partition(" ")
            if key == "inactive_file":
                return int(value)
    except (OSError, ValueError):
        pass
    return 0


def cgroup_memory_usage_bytes() -> int | None:
    # AUDIT R2: memory.current includes the file cache — under mem_limit=480m
    # the headroom could honestly drop below 20 MB without real pressure,
    # killing the process in a restart loop. Subtract inactive_file.
    for usage_path, stat_path in (
        (Path("/sys/fs/cgroup/memory.current"), Path("/sys/fs/cgroup/memory.stat")),
        (
            Path("/sys/fs/cgroup/memory/memory.usage_in_bytes"),
            Path("/sys/fs/cgroup/memory/memory.stat"),
        ),
    ):
        try:
            usage = int(usage_path.read_text().strip())
        except (OSError, ValueError):
            continue
        return max(0, usage - _inactive_file_bytes(stat_path))
    return None
