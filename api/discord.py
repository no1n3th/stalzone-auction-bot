"""Discord notifier: priority queue, concurrent webhooks, chart attach via edit.

Fast path: embeds go out without images; a chart is attached later by editing
the same webhook message (PATCH .../messages/{id}) once the PNG is rendered.
On 429 the message waits `retry_after` and is retried — never dropped to DLQ
just for rate limiting. DLQ payloads store the chart file path, not bytes.
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
import json
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

import aiohttp
import structlog

from api.client import get_icon_url
from config.settings import QUALITY_NAMES, Settings
from models.events import EventType, NotificationEvent
from models.lot import Lot
from utils.helpers import format_money
from utils.rate_limiter import TokenBucket

log = structlog.get_logger(__name__)

# STALCRAFT rarity palette (not the WoW one): white/green/blue/purple/red/gold
RARITY_STYLE: dict[int, dict[str, Any]] = {
    0: {"name": "Обычная", "color": 0xD4D4D4},
    1: {"name": "Необычная", "color": 0x5FD94A},
    2: {"name": "Редкая", "color": 0x3FA9FF},
    3: {"name": "Особая", "color": 0xB06BFF},
    4: {"name": "Исключительная", "color": 0xFF5C7A},
    5: {"name": "Легендарная", "color": 0xFFC94D},
}

EVENT_STYLE: dict[EventType, dict[str, Any]] = {
    # P0-5/редизайн 4.6: цвет кодирует тип события, эмодзи убраны; prefix —
    # вместо эмодзи в заголовке.
    EventType.LOT: {"prefix": "Лот", "color": 0x5DBB8A},
    EventType.MID_LOT: {"prefix": "Промежуточная заточка", "color": 0x6FA3D8},
    EventType.DUMP: {"prefix": "Дамп", "color": 0xE5646B},
    EventType.META_EXIT: {"prefix": "Мета: выход", "color": 0xD8A657},
    EventType.META_ENTER: {"prefix": "Мета: вход", "color": 0x9AA5D6},
    EventType.SEASON_PREWARN: {"prefix": "Сезон: скоро старт", "color": 0x7C858E},
    EventType.SEASON_START: {"prefix": "Сезон: начало", "color": 0x7C858E},
    EventType.SEASON_END: {"prefix": "Сезон: конец", "color": 0x7C858E},
    EventType.SEASONAL_DROP: {"prefix": "Сезонное падение", "color": 0x7C858E},
    EventType.SEASON_DIP_BUY: {"prefix": "Dip-buy", "color": 0x5DBB8A},
}

# send priority: profitable lots first, season transitions last
PRIORITY_LOT = 0
PRIORITY_DIP_BUY = 1
PRIORITY_DUMP = 2
PRIORITY_META = 3
PRIORITY_SEASON = 4

EVENT_PRIORITY: dict[EventType, int] = {
    EventType.LOT: PRIORITY_LOT,
    EventType.MID_LOT: PRIORITY_LOT,
    EventType.SEASON_DIP_BUY: PRIORITY_DIP_BUY,
    EventType.DUMP: PRIORITY_DUMP,
    EventType.SEASONAL_DROP: PRIORITY_DUMP,
    EventType.META_EXIT: PRIORITY_META,
    EventType.META_ENTER: PRIORITY_META,
    EventType.SEASON_PREWARN: PRIORITY_SEASON,
    EventType.SEASON_START: PRIORITY_SEASON,
    EventType.SEASON_END: PRIORITY_SEASON,
}

SEASON_NAMES_RU: dict[str, str] = {
    "winter": "Зима",
    "spring": "Весна",
    "summer": "Лето",
    "autumn": "Осень",
}

#: human labels for NotificationEvent.fields keys
FIELD_LABELS: dict[str, str] = {
    "season": "Сезон",
    "quality": "Качество",
    "upgrade": "Заточка",
    "price": "Цена",
    "avg": "Средняя до выхода",
    "avg_now": "Средняя сейчас",
    "avg_prev": "Средняя ранее",
    "preseason_avg": "Предсезонная средняя",
    "dip_line": "Линия dip-buy",
    "drop_pct": "Падение",
    "surge_pct": "Рост продаж",
    "cheap_lots": "Дешёвых лотов",
    "sales_now": "Продаж сейчас",
    "sales_prev": "Продаж ранее",
    "fast_adapt_weight": "Ускорение адаптации",
    "window_days": "Окно истории",
    "prewarn_days": "Дней до начала",
    "starts": "Начало",
    "note": "Примечание",
}

_MONEY_KEYS = {"price", "avg", "avg_now", "avg_prev", "preseason_avg", "dip_line"}
_PCT_KEYS = {"drop_pct", "surge_pct"}
_DAYS_KEYS = {"window_days", "prewarn_days"}

MAX_429_RETRIES = 3
ATTACH_RETRY_DELAY_SEC = 1.0  # P1-14: 2s total waited for the sent-index and
ATTACH_MAX_ATTEMPTS = 10      # produced a duplicate "chart" message too often
SENT_INDEX_TTL_SEC = 600.0


def _fmt_money(value: Any) -> str:
    """P0-5: single money format for the whole service (utils.helpers)."""
    return format_money(value)


def _fmt_field(key: str, value: Any) -> str:
    if key == "season":
        return SEASON_NAMES_RU.get(str(value), str(value))
    if key == "quality":
        return QUALITY_NAMES.get(int(value), str(value)) if str(value).isdigit() else str(value)
    if key == "upgrade":
        level = int(value) if str(value).lstrip("-").isdigit() else 0
        return f"+{level}" if level else "без заточки"
    if key in _MONEY_KEYS:
        return _fmt_money(value)
    if key in _PCT_KEYS:
        return f"{float(value):g}%"
    if key in _DAYS_KEYS:
        return f"{value} дн."
    if key == "fast_adapt_weight":
        return f"×{float(value):g}"
    return str(value)


def icon_url(path: str) -> str:
    """P0-1: icons in stalcraft-database live under ru/icons/<subtype>/ — the
    listing entry's own `icon` field carries the exact relative path
    (e.g. 'icons/artefact/anomaly/abc.png'). Never guess flat paths."""
    p = path.lstrip("/")
    if p.startswith("ru/"):  # tolerate both 'icons/...' and 'ru/icons/...'
        p = p[len("ru/"):]
    return "https://raw.githubusercontent.com/EXBO-Studio/stalcraft-database/main/ru/" + p


DLQFactory = Callable[[dict[str, Any]], Awaitable[None]]


class DiscordNotifier:
    def __init__(
        self,
        settings: Settings,
        session: aiohttp.ClientSession,
        dlq_factory: DLQFactory | None = None,
        template_path: Path | None = None,
        metrics: Any = None,  # AUDIT Q4: optional MetricsCollector
    ) -> None:
        self.settings = settings
        self.session = session
        self.metrics = metrics
        self.dlq_factory = dlq_factory
        self.queue: asyncio.PriorityQueue[tuple[int, float, int, dict[str, Any]]] = (
            asyncio.PriorityQueue(maxsize=settings.ws_feed_queue_size * 5)
        )
        self._seq = itertools.count()
        self._sent = 0
        self._failed = 0
        # Discord-wide token bucket: stay well under the 50 req/s global limit
        self._rl = TokenBucket(
            capacity=settings.discord_rate_per_sec, refill_rate=float(settings.discord_rate_per_sec)
        )
        # lot_sig -> [(webhook, message_id, embed)] for the deferred chart attach
        self._sent_index: dict[str, tuple[float, list[tuple[str, str, dict[str, Any]]]]] = {}
        self.chart_dir: Path | None = None  # set by the container (DLQ chart persistence)
        self.icons: Mapping[str, str] = {}  # P0-1: item_id -> raw icon path (listing)
        self.paused: bool = False  # P1-10: admin flag 'discord_enabled' -> notifier.paused

    # ---------- embeds ----------
    def build_embed(
        self,
        lot: Lot,
        item_name: str,
        avg_price: float | None,
        next_price: float | None,
        market_min: float | None,
        title: str | None = None,
    ) -> dict[str, Any]:
        fee = self.settings.fee
        quality = max(0, min(lot.quality, 5))
        style = RARITY_STYLE[quality]
        profit_next = (
            round(next_price * (1 - fee) - lot.price, 2) if next_price is not None else None
        )
        profit_avg_pct = (
            round((avg_price * (1 - fee) - lot.price) / lot.price * 100.0, 1)
            if avg_price is not None and lot.price > 0
            else None
        )
        fields: list[dict[str, Any]] = [
            {"name": "Качество", "value": style["name"], "inline": True},
            {"name": "Цена", "value": format_money(lot.price), "inline": True},
        ]
        if market_min is not None:
            fields.append(
                {"name": "Минимум рынка", "value": format_money(market_min), "inline": True}
            )
        if avg_price is not None:
            fields.append(
                {"name": "Средняя (окно)", "value": format_money(avg_price), "inline": True}
            )
        if next_price is not None:
            fields.append(
                {"name": "Следующий лот", "value": format_money(next_price), "inline": True}
            )
        if profit_next is not None:
            fields.append(
                {"name": "Профит к след.", "value": format_money(profit_next), "inline": True}
            )
        if profit_avg_pct is not None:
            sign = "+" if profit_avg_pct >= 0 else "\u2212"
            fields.append(
                {"name": "К средней", "value": f"{sign}{abs(profit_avg_pct):g}%", "inline": True}
            )
        fields.append({"name": "Комиссия", "value": f"{self.settings.fee_pct:g}%", "inline": True})
        display = item_name or self.icons.get("__name__", {}).get(lot.item_id, lot.item_id)
        name = f"{display} +{lot.upgrade}" if lot.upgrade else display
        embed: dict[str, Any] = {
            "title": (title or name)[:256],
            "color": style["color"],  # P0-5: rarity color really applies now
            "fields": fields[:25],
            "footer": {"text": "Stalzone"},
            "timestamp": datetime.now(UTC).isoformat(),
        }
        # P0-1/P0-5: prefer the listing's exact icon path; fall back to the
        # constructed ru/icons/artefact/<id>.png (well-formed https — Discord
        # shows a placeholder for a missing image instead of rejecting the
        # whole webhook with a 400 the way an empty URL does).
        icon_path = self.icons.get(lot.item_id)
        embed["thumbnail"] = {"url": icon_url(icon_path) if icon_path else get_icon_url(lot.item_id)}
        return embed

    def build_event_embed(
        self,
        event: NotificationEvent,
        item_name: str = "",
        item_names: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        style = EVENT_STYLE.get(event.event_type, {"prefix": "Событие", "color": 0x7C858E})
        title = event.title
        if item_name and event.item_id:
            title = title.replace(event.item_id, item_name)
        for eng, rus in SEASON_NAMES_RU.items():
            title = title.replace(f": {eng}", f": {rus}")
        fields = [
            {
                "name": FIELD_LABELS.get(str(k), str(k)),
                "value": _fmt_field(str(k), v),
                "inline": True,
            }
            for k, v in event.fields.items()
            if k != "items"
        ]
        if "items" in event.fields:
            ids = [str(i) for i in event.fields["items"]]
            names = [item_names.get(i, i) if item_names else i for i in ids]
            value = ", ".join(names)
            if len(value) > 1024:
                shown: list[str] = []
                total = 0
                for n in names:
                    if total + len(n) + 2 > 900:
                        break
                    shown.append(n)
                    total += len(n) + 2
                value = ", ".join(shown) + f" … и ещё {len(names) - len(shown)}"
            fields.append({"name": "Предметы", "value": value or "—", "inline": False})
        # сезонные события уже несут смысл в заголовке ("Сезон начался: …") —
        # префикс дублировал бы его ("Сезон: начало: Сезон начался: …")
        prefix = "" if event.event_type.value.startswith("season") else f"{style['prefix']}: "
        embed: dict[str, Any] = {
            "title": f"{prefix}{title}"[:256],
            "color": style["color"],
            "fields": fields,
            "timestamp": event.detected_at.isoformat(),
        }
        if not item_name and event.item_id:
            embed["description"] = event.item_id
        return embed

    # ---------- queue ----------
    # AUDIT R4-outbox: optional durable-log hooks, wired by the container.
    _outbox_add: Any = None  # async (payload_json: str) -> int (row id)
    _outbox_done: Any = None  # async (row_id: int) -> None
    _bg_tasks: ClassVar[set[asyncio.Task[None]]] = set()  # strong refs for fire-and-forget tasks

    async def enqueue(
        self,
        lot_sig: str,
        embed: dict[str, Any],
        detected_at: datetime,
        chart_png: bytes | None = None,
        *,
        priority: int = PRIORITY_SEASON,
        chart_key: str | None = None,
        persist: bool = True,
        outbox_id: int | None = None,
    ) -> None:
        if self.paused:  # P1-10: notifications paused from the admin UI
            log.info("discord_paused_drop", lot_sig=lot_sig)
            return
        embed = {**embed, "fields": embed.get("fields", [])}  # Discord требует ключ
        payload: dict[str, Any] = {
            "lot_sig": lot_sig,
            "embed": embed,
            "detected_at": detected_at.isoformat(),
            "chart_png": chart_png,
            "chart_key": chart_key,
        }
        if persist and self._outbox_add is not None:
            # P0-2.3: persist synchronously BEFORE the queue handoff so the row
            # id is known at send time (was a fire-and-forget task racing the
            # sender; bytes are never JSON-dumped into the outbox).
            durable: dict[str, Any] = {k: v for k, v in payload.items() if k != "chart_png"}
            durable["priority"] = priority
            durable["event_type"] = embed.get("event_type", "")
            try:
                outbox_id = await self._outbox_add(json.dumps(durable, ensure_ascii=False))
            except Exception:
                log.warning("outbox_persist_failed")
        if outbox_id is not None:
            payload["_outbox_id"] = outbox_id  # shared with the queue item
        ts = detected_at.timestamp()
        item = (priority, ts, next(self._seq), payload)
        try:
            self.queue.put_nowait(item)
        except asyncio.QueueFull:
            # overflow: evict the *least important* queued event, never a fresh lot
            if self._evict_lower_priority(priority):
                try:
                    self.queue.put_nowait(item)
                    return
                except asyncio.QueueFull:
                    pass
            log.warning("discord_queue_full_drop", lot_sig=lot_sig, priority=priority)

    def _evict_lower_priority(self, new_priority: int) -> bool:
        """Drop the worst-priority queued item if it is less urgent than the new one."""
        items = self.queue._queue  # type: ignore[attr-defined]  # heap of (priority, ts, seq, payload); same loop thread
        if not items:
            return False
        worst_i, worst = max(enumerate(items), key=lambda kv: (kv[1][0], kv[1][1]))
        if worst[0] <= new_priority:
            return False  # nothing less important than the newcomer
        items[worst_i] = items[-1]
        items.pop()
        heapq.heapify(items)
        # P1-14: evicted item never gets task_done(); PriorityQueue does not
        # declare the counter in its stubs.
        self.queue.unfinished_tasks -= 1  # type: ignore[attr-defined]
        log.info("discord_queue_evicted", evicted_priority=worst[0])
        return True

    async def _retry_after(self, resp: aiohttp.ClientResponse) -> float:
        """P1-14: Discord JSON body or Retry-After header (Cloudflare 429 = HTML)."""
        try:
            data = await resp.json(content_type=None)
            if isinstance(data, dict) and data.get("retry_after") is not None:
                return min(float(data["retry_after"]), 30.0)
        except Exception:
            pass
        try:
            return min(float(resp.headers.get("Retry-After", "1.0")), 30.0)
        except ValueError:
            return 5.0

    # ---------- sending ----------
    async def _post_hook(
        self, hook: str, embed: dict[str, Any], chart_png: bytes | None
    ) -> tuple[bool, str | None]:
        """POST one embed to one webhook; 429 -> wait retry_after and retry same message.

        Returns (ok, message_id). wait=true so Discord returns the created message.
        """
        body: dict[str, Any] = {"embeds": [embed]}
        url = f"{hook}?wait=true"
        for _attempt in range(MAX_429_RETRIES + 1):
            await self._rl.acquire()
            try:
                if chart_png:
                    form = aiohttp.FormData()
                    form.add_field(
                        "payload_json", json.dumps(body), content_type="application/json"
                    )
                    form.add_field(
                        "files[0]", chart_png, filename="chart.png", content_type="image/png"
                    )
                    resp_cm = self.session.post(url, data=form)
                else:
                    resp_cm = self.session.post(url, json=body)
                async with resp_cm as resp:
                    if resp.status == 429:
                        retry_after = await self._retry_after(resp)
                        log.warning("discord_429_retry", retry_after=retry_after, attempt=_attempt)
                        await asyncio.sleep(min(retry_after, 30.0))
                        continue  # retry THIS message — no DLQ for rate limiting
                    if resp.status >= 400:
                        log.error("discord_send_failed", status=resp.status)
                        return False, None
                    self._sent += 1
                    message_id: str | None = None
                    try:
                        data = await resp.json()
                        message_id = str(data.get("id")) if isinstance(data, dict) else None
                    except Exception:
                        message_id = None
                    return True, message_id
            except (TimeoutError, aiohttp.ClientError) as exc:
                log.error("discord_send_error", error=str(exc))
                return False, None
        return False, None

    async def _send_one(self, payload: dict[str, Any]) -> bool:
        hooks = self.settings.discord_webhooks
        if not hooks:
            log.info("discord_no_webhook_skip")
            return True
        embed = payload["embed"]
        chart_png = payload.get("chart_png")
        # P0-2.10: per-webhook delivery — a hook that already accepted the
        # message is never POSTed again on retry (was: gather + all(...) ->
        # the retry hit every hook, duplicating the message on the ones that
        # had already succeeded).
        pending_hooks = list(hooks)
        delivered: list[tuple[str, str | None]] = []
        ok = True
        for _round in range(MAX_429_RETRIES + 1):
            results = await asyncio.gather(
                *(self._post_hook(h, embed, chart_png) for h in pending_hooks),
                return_exceptions=True,
            )
            retry_hooks: list[str] = []
            for hook, res in zip(pending_hooks, results, strict=False):
                if isinstance(res, BaseException):
                    retry_hooks.append(hook)
                    ok = False
                    continue
                sent_ok, mid = res
                if sent_ok:
                    delivered.append((hook, mid))
                else:
                    retry_hooks.append(hook)
                    ok = False
            pending_hooks = retry_hooks
            if not pending_hooks:
                break
        # AUDIT R4-outbox: delivery confirmed on every webhook -> close the row.
        outbox_id = payload.get("_outbox_id")
        if ok and outbox_id is not None and self._outbox_done is not None:
            await self._outbox_done(outbox_id)  # P0-2: close the row in-line
        # remember message ids when a deferred chart may follow
        if payload.get("chart_key") and payload.get("lot_sig"):
            indexed = [(hook, mid, embed) for hook, mid in delivered if mid]
            if indexed:
                self._sent_index[payload["lot_sig"]] = (
                    datetime.now(UTC).timestamp(),
                    indexed,
                )
                self._gc_sent_index()
        return ok

    async def _send_batch(self, batch: list[dict[str, Any]]) -> None:
        for payload in batch:
            if await self._send_one(payload):
                continue
            self._failed += 1
            # DECISIONS D7: the outbox is the single source of truth for
            # retries. Outbox-backed payloads re-enter via outbox_sweep_worker;
            # pushing them into the DLQ too used to replay every failure twice.
            if payload.get("_outbox_id") is not None:
                continue
            if self.dlq_factory is not None:
                try:
                    await self.dlq_factory(self._dlq_payload(payload))
                except Exception as exc:
                    log.error("dlq_push_failed", error=str(exc))

    def _dlq_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        """DLQ entries keep the chart *path*, not the PNG bytes."""
        out = {k: v for k, v in payload.items() if k != "chart_png"}
        png = payload.get("chart_png")
        if png and self.chart_dir is not None:
            try:
                self.chart_dir.mkdir(parents=True, exist_ok=True)
                name = f"dlq_{payload.get('chart_key') or payload.get('lot_sig') or 'chart'}.png"
                path = self.chart_dir / name
                path.write_bytes(png)
                out["chart_path"] = str(path)
            except OSError as exc:
                log.warning("dlq_chart_save_failed", error=str(exc))
        return out

    async def run_sender(self, stop: asyncio.Event, heartbeat: Any = None) -> None:
        """Batch sender loop; drains the priority queue on shutdown."""
        while not stop.is_set() or not self.queue.empty():
            if heartbeat is not None:
                heartbeat.beat("discord_sender")
            batch: list[dict[str, Any]] = []
            try:
                first = await asyncio.wait_for(self.queue.get(), timeout=1.0)
                batch.append(first[3])
                while len(batch) < self.settings.notify_batch_size:
                    batch.append(self.queue.get_nowait()[3])
            except (TimeoutError, asyncio.QueueEmpty):
                pass
            if batch:
                await self._send_batch(batch)

    async def replay_dlq(self, payloads: list[dict[str, Any]]) -> tuple[int, int]:
        """Returns (sent, failed); restores chart bytes from the cached file."""
        sent = failed = 0
        for payload in payloads:
            chart_path = payload.get("chart_path")
            if chart_path and not payload.get("chart_png"):
                try:
                    payload["chart_png"] = Path(chart_path).read_bytes()
                except OSError:
                    payload["chart_png"] = None
            if await self._send_one(payload):
                sent += 1
            else:
                failed += 1
        return sent, failed

    # ---------- deferred chart attach ----------
    async def attach_chart(
        self, lot_sig: str, png: bytes, max_attempts: int = ATTACH_MAX_ATTEMPTS
    ) -> bool:
        """Attach a rendered chart to the already-sent message (PATCH), else follow-up."""
        for _attempt in range(max_attempts):
            entry = self._sent_index.get(lot_sig)
            if entry is not None:
                break
            await asyncio.sleep(ATTACH_RETRY_DELAY_SEC)  # message may still be in flight
        else:
            entry = None
        if entry is None:
            log.info("chart_attach_no_message", lot_sig=lot_sig)
            return await self._followup_chart(png)
        _, indexed = entry
        ok = True
        for hook, message_id, embed in indexed:
            if not await self._patch_message(hook, message_id, embed, png):
                ok = await self._followup_chart(png, hook=hook) and ok
        self._sent_index.pop(lot_sig, None)
        return ok

    async def _patch_message(
        self, hook: str, message_id: str, embed: dict[str, Any], png: bytes
    ) -> bool:
        """Edit the original webhook message, adding the chart image to its embed."""
        edited = dict(embed)
        edited["image"] = {"url": "attachment://chart.png"}
        url = f"{hook}/messages/{message_id}"
        for _attempt in range(MAX_429_RETRIES + 1):
            # AUDIT R7: FormData is single-use — a consumed body raises
            # RuntimeError when the 429 retry re-sends it. Rebuild per attempt.
            form = aiohttp.FormData()
            form.add_field(
                "payload_json",
                json.dumps({"embeds": [edited], "attachments": []}),
                content_type="application/json",
            )
            form.add_field("files[0]", png, filename="chart.png", content_type="image/png")
            await self._rl.acquire()
            try:
                async with self.session.patch(url, data=form) as resp:
                    if resp.status == 429:
                        retry_after = await self._retry_after(resp)
                        await asyncio.sleep(min(retry_after, 30.0))
                        continue
                    if resp.status >= 400:
                        log.warning("chart_patch_failed", status=resp.status)
                        return False
                    return True
            except (TimeoutError, aiohttp.ClientError) as exc:
                log.warning("chart_patch_error", error=str(exc))
                return False
        return False

    async def _followup_chart(self, png: bytes, hook: str | None = None) -> bool:
        """Fallback: a separate message with the chart when editing is impossible."""
        hooks = [hook] if hook else self.settings.discord_webhooks
        if not hooks:
            return False
        embed = {
            "title": "График цены",
            "color": 0x7C858E,
            "image": {"url": "attachment://chart.png"},
            "timestamp": datetime.now(UTC).isoformat(),
        }
        results = await asyncio.gather(
            *(self._post_hook(h, embed, png) for h in hooks), return_exceptions=True
        )
        return any(r is not False and isinstance(r, tuple) and r[0] for r in results)  # type: ignore[comparison-overlap]

    def _gc_sent_index(self) -> None:
        cutoff = datetime.now(UTC).timestamp() - SENT_INDEX_TTL_SEC
        stale = [k for k, (ts, _) in self._sent_index.items() if ts < cutoff]
        for k in stale:
            self._sent_index.pop(k, None)
