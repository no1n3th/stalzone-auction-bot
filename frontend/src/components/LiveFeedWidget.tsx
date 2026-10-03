import { useEffect, useRef, useState } from "react";
import { isPing } from "../lib/ping";

interface FeedEvent {
  event_type: string;
  item_id: string;
  title: string;
  fields: Record<string, unknown>;
  detected_at: string;
}

/** Live feed widget: floating panel with the latest WS events. */
export default function LiveFeedWidget() {
  const [events, setEvents] = useState<FeedEvent[]>([]);
  const [open, setOpen] = useState(false);
  const [connected, setConnected] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    let cancelled = false;
    let retryTimer: number | undefined;
    let retryDelay = 5000;

    const connect = () => {
      const proto = window.location.protocol === "https:" ? "wss" : "ws";
      const ws = new WebSocket(`${proto}://${window.location.host}/api/ws/feed`);
      wsRef.current = ws;
      ws.onopen = () => {
        retryDelay = 5000; // F-2: reset ONLY on success (was: at connect start, so backoff never grew)
        setConnected(true);
      };
      ws.onmessage = (msg) => {
        try {
          const data: unknown = JSON.parse(msg.data as string);
          if (isPing(data)) {
            return; // server heartbeat — not a feed event
          }
          const event = data as FeedEvent;
          setEvents((prev) => [event, ...prev].slice(0, 15));
        } catch {
          /* ignore malformed frames */
        }
      };
      ws.onclose = (ev) => {
        setConnected(false);
        if (cancelled || ev.code === 4401) return; // 4401 = session revoked, stop reconnecting
        retryDelay = Math.min(retryDelay * 2, 60000);
        retryTimer = window.setTimeout(connect, retryDelay + Math.random() * 1000); // F-2: jitter
      };
    };
    connect();
    return () => {
      cancelled = true;
      if (retryTimer) window.clearTimeout(retryTimer);
      wsRef.current?.close();
    };
  }, []);

  return (
    <div className="fixed bottom-4 right-4 z-30 w-80">
      <button
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        className="card ml-auto flex items-center gap-2 px-4 py-2 text-sm"
      >
        <span className={`live-dot ${connected ? "" : "opacity-30"}`} />
        Live-лента
        <span className="text-xs text-[var(--subtle)]">{connected ? "онлайн" : "нет связи"}</span>
        {events.length > 0 && (
          <span className="badge border-gold/40 text-gold">{events.length}</span>
        )}
      </button>
      {/* a11y: закрытая панель не должна оставаться в порядке Tab */}
      {open && (
      <div className="mt-2 origin-bottom-right">
        <div className="card max-h-96 overflow-y-auto p-3">
          {events.length === 0 ? (
            <p className="p-2 text-sm text-ink/50">Событий пока нет — ждём сигналы…</p>
          ) : (
            events.map((e, i) => (
              <div key={`${e.detected_at}-${i}`} className="anim-feed-item border-b border-ink/5 px-2 py-2 text-sm last:border-0">
                <div className="flex items-center gap-2">
                  <span className="badge">{e.event_type}</span>
                  <span className="truncate font-medium">{e.title}</span>
                </div>
                <div className="mt-0.5 text-xs text-ink/45">
                  {new Date(e.detected_at).toLocaleTimeString("ru-RU")}
                </div>
              </div>
            ))
          )}
        </div>
      </div>
      )}
    </div>
  );
}
