import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, type Artifact, type Notification } from "../api";
import { playNotificationSound } from "../lib/sound";

const EVENT_COLOR: Record<string, string> = {
  lot: "var(--profit)", mid_lot: "var(--accent)", dump: "var(--loss)",
  meta_enter: "var(--accent)", meta_exit: "var(--warn)",
  season_prewarn: "var(--subtle)", season_start: "var(--subtle)",
  season_end: "var(--subtle)", seasonal_drop: "var(--warn)", season_dip_buy: "var(--profit)",
};

const FIELD_LABELS: Record<string, string> = {
  season: "сезон", items: "предметы", window_days: "окно, дн",
  fast_adapt_weight: "ускорение", quality: "качество", upgrade: "заточка",
  price: "цена", avg: "средняя", drop_pct: "падение, %", surge_pct: "рост, %",
  cheap_lots: "лотов ниже", sales_now: "продаж сейчас", sales_prev: "продаж ранее",
  preseason_avg: "предсезонная", dip_line: "линия dip-buy",
};

function formatFields(fields: Record<string, unknown> | undefined, names: Record<string, string>): string {
  if (!fields) return "—";
  const parts: string[] = [];
  for (const [k, v] of Object.entries(fields)) {
    if (k === "items" && Array.isArray(v)) {
      parts.push(`${FIELD_LABELS[k] ?? k}: ${(v as string[]).map((id) => names[id] ?? id).join(", ")}`);
    } else {
      parts.push(`${FIELD_LABELS[k] ?? k}: ${typeof v === "number" ? v : String(v)}`);
    }
  }
  return parts.join(" · ");
}

function relative(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime();
  const m = Math.floor(diff / 60000);
  if (m < 1) return "только что";
  if (m < 60) return `${m} мин назад`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h} ч назад`;
  return `${Math.floor(h / 24)} дн назад`;
}

/** Извлечь item_id из события (из payload или title). */
function extractItemId(e: Notification): string | null {
  const payload = e.payload as { item_id?: string; title?: string };
  if (payload.item_id) return payload.item_id;
  const title = payload.title ?? "";
  const m = title.match(/\b[a-z0-9]{4,5}\b/);
  return m ? m[0] : null;
}

/** Треугольник тренда по изменению цены. */
function TrendTriangle({ change }: { change: number }) {
  if (change > 0.5) return <span className="trend trend-up" title={`+${change.toFixed(1)}%`}>▲</span>;
  if (change < -0.5) return <span className="trend trend-down" title={`${change.toFixed(1)}%`}>▼</span>;
  return <span className="trend trend-flat" title="без изменений">■</span>;
}

/** Мини-спарклайн SVG. */
function Sparkline({ points, width = 60, height = 16 }: { points: number[]; width?: number; height?: number }) {
  if (points.length < 2) return <span style={{ width, display: "inline-block" }} />;
  const min = Math.min(...points), max = Math.max(...points);
  const range = max - min || 1;
  const step = width / (points.length - 1);
  const d = points.map((p, i) => `${i * step},${height - 2 - ((p - min) / range) * (height - 4)}`).join(" ");
  return (
    <svg width={width} height={height} className="inline-block opacity-70">
      <polyline points={d} fill="none" stroke="var(--accent)" strokeWidth="1.2" />
    </svg>
  );
}

interface DrawerData {
  itemId: string;
  name: string;
  daily: { day: string; price: number }[];
  change24h: number;
}

export default function FeedPage() {
  const [events, setEvents] = useState<Notification[] | null>(null);
  const [names, setNames] = useState<Record<string, string>>({});
  const [artifacts, setArtifacts] = useState<Record<string, Artifact>>({});
  const [error, setError] = useState<string | null>(null);
  const [drawer, setDrawer] = useState<DrawerData | null>(null);
  const [soundPrefs, setSoundPrefs] = useState<{ enabled: boolean; volume: number; events: string[]; threshold: number } | null>(null);
  const [freshIds, setFreshIds] = useState<Set<number>>(new Set());

  useEffect(() => {
    api.notifications(200).then(setEvents).catch((e) => setError(e instanceof Error ? e.message : "ошибка"));
    api.artifacts().then((list) => {
      setNames(Object.fromEntries(list.map((a) => [a.item_id, a.name])));
      setArtifacts(Object.fromEntries(list.map((a) => [a.item_id, a])));
    }).catch(() => undefined);
    api.getSettings().then((s) => {
      setSoundPrefs({ enabled: s.sound_enabled, volume: s.sound_volume, events: s.sound_events, threshold: s.profit_threshold_pct });
    }).catch(() => undefined);
  }, []);

  // WebSocket: обновление ленты + звук
  useEffect(() => {
    const proto = window.location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${window.location.host}/api/ws/feed`);
    ws.onmessage = (msg) => {
      try {
        const data = JSON.parse(msg.data as string);
        if (data.type === "ping") return;
        const event = data as Notification;
        setEvents((prev) => [event, ...(prev ?? [])].slice(0, 200));
        // помечаем как свежее: подсветка + мигание
        setFreshIds((prev) => new Set(prev).add(event.id));
        setTimeout(() => setFreshIds((prev) => { const n = new Set(prev); n.delete(event.id); return n; }), 700);
        // звук только если вкладка скрыта и настройки позволяют
        if (soundPrefs?.enabled && document.visibilityState !== "visible") {
          const profit = (event.payload as { fields?: Record<string, unknown> }).fields?.profit_pct;
          const profitNum = typeof profit === "number" ? profit : 0;
          if (soundPrefs.events.includes(event.event_type) && profitNum >= soundPrefs.threshold) {
            playNotificationSound(soundPrefs.volume);
          }
        }
      } catch { /* ignore */ }
    };
    return () => ws.close();
  }, [soundPrefs]);

  async function openDrawer(itemId: string) {
    const art = artifacts[itemId];
    const q = art?.min_quality ?? 0;
    const u = art?.upgrades?.length ? Math.min(...art.upgrades) : 0;
    try {
      const daily = await api.dailyPrices(itemId, q, u);
      const points = daily.map((d) => d.avg_price);
      const change = points.length >= 2
        ? ((points[points.length - 1] - points[points.length - 2]) / points[points.length - 2]) * 100
        : 0;
      setDrawer({
        itemId, name: names[itemId] ?? itemId,
        daily: daily.map((d) => ({ day: d.day.slice(5), price: d.avg_price })),
        change24h: change,
      });
    } catch {
      setDrawer({ itemId, name: names[itemId] ?? itemId, daily: [], change24h: 0 });
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex items-end justify-between gap-3">
        <div>
          <h1>Лента событий</h1>
          <p className="sub">Клик по предмету — детали справа</p>
        </div>
      </div>
      {error && <p className="err">{error}</p>}

      {events === null && !error && (
        <div className="card-flat p-3 space-y-2">{[...Array(4)].map((_, i) => <div key={i} className="skeleton h-6" />)}</div>
      )}
      {events && events.length === 0 && !error && (
        <div className="card-flat"><p className="empty">Событий пока нет — сканер работает.</p></div>
      )}
      {events && events.length > 0 && (
        <div className="card-flat overflow-x-auto p-0">
          <table className="table">
            <thead>
              <tr><th className="w-8"></th><th>Событие</th><th>Детали</th><th className="text-right">Время</th></tr>
            </thead>
            <tbody>
              {events.map((e) => {
                const fields = (e.payload as { fields?: Record<string, unknown> }).fields;
                const itemId = extractItemId(e);
                const isNew = freshIds.has(e.id);
                return (
                  <tr key={e.id} className={`row anim-feed-item ${isNew ? "anim-flash" : ""}`}>
                    <td><span className="inline-block h-3 w-1 rounded-full" style={{ background: EVENT_COLOR[e.event_type] ?? "var(--subtle)" }} /></td>
                    <td className="font-medium">
                      {itemId ? (
                        <button className="hover:underline text-left" onClick={() => openDrawer(itemId)}>
                          {(e.payload as { title?: string }).title ?? e.event_type}
                        </button>
                      ) : (
                        (e.payload as { title?: string }).title ?? e.event_type
                      )}
                    </td>
                    <td className="text-muted">{formatFields(fields, names)}</td>
                    <td className="text-right text-muted whitespace-nowrap">{relative(e.created_at)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {/* Drawer */}
      <div className={`drawer-overlay ${drawer ? "open" : ""}`} onClick={() => setDrawer(null)} />
      <div className={`drawer ${drawer ? "open" : ""}`} role="dialog" aria-label="Детали предмета">
        {drawer && (
          <>
            <div className="drawer-header">
              <div className="flex items-center gap-2">
                <TrendTriangle change={drawer.change24h} />
                <h2>{drawer.name}</h2>
              </div>
              <button className="btn btn-ghost btn-sm" onClick={() => setDrawer(null)}>✕</button>
            </div>
            <div className="drawer-body space-y-4">
              {drawer.daily.length >= 2 ? (
                <>
                  <div className="flex items-center justify-between text-sm">
                    <span className="text-muted">Изменение за период:</span>
                    <span className={drawer.change24h >= 0 ? "text-[var(--profit)]" : "text-[var(--loss)]"}>
                      {drawer.change24h >= 0 ? "+" : ""}{drawer.change24h.toFixed(1)}%
                    </span>
                  </div>
                  <div className="h-40">
                    <Sparkline points={drawer.daily.map((d) => d.price)} width={360} height={140} />
                  </div>
                  <div className="forecast-row">
                    {drawer.daily.slice(-3).map((d, i) => (
                      <div key={i} className="forecast-cell">
                        <div className="f-day">{i === 0 ? "сегодня" : i === 1 ? "вчера" : `${i} дн назад`}</div>
                        <div className="f-val">{d.price.toLocaleString("ru-RU")}</div>
                      </div>
                    ))}
                  </div>
                </>
              ) : (
                <p className="empty">Недостаточно данных для графика</p>
              )}
              <div className="flex gap-2 pt-2">
                <Link to={`/market`} className="btn btn-primary flex-1" onClick={() => setDrawer(null)}>
                  Открыть на рынке
                </Link>
              </div>
              <p className="mono">id: {drawer.itemId}</p>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
