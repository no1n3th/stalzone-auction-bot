import { useEffect, useMemo, useState } from "react";
import { api, type Artifact, type DailyPrice, type MarketPrice } from "../api";
import {
  CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";

type Period = "24h" | "3d" | "week" | "month" | "year";
const PERIODS: [Period, string, number][] = [
  ["24h", "24 ч", 1], ["3d", "3 дн", 3], ["week", "нед", 7],
  ["month", "мес", 30], ["year", "год", 365],
];

function TrendTriangle({ change }: { change: number }) {
  if (change > 0.5) return <span className="trend trend-up" title={`+${change.toFixed(1)}% за 24ч`}>▲</span>;
  if (change < -0.5) return <span className="trend trend-down" title={`${change.toFixed(1)}% за 24ч`}>▼</span>;
  return <span className="trend trend-flat" title="без изменений">■</span>;
}

function Sparkline({ points, width = 56, height = 16 }: { points: number[]; width?: number; height?: number }) {
  if (points.length < 2) return <span style={{ width, display: "inline-block" }} />;
  const min = Math.min(...points), max = Math.max(...points);
  const range = max - min || 1;
  const step = width / (points.length - 1);
  const d = points.map((p, i) => `${i * step},${height - 2 - ((p - min) / range) * (height - 4)}`).join(" ");
  return (
    <svg width={width} height={height} className="inline-block opacity-60">
      <polyline points={d} fill="none" stroke="var(--accent)" strokeWidth="1.2" />
    </svg>
  );
}

function shortDay(day: string): string {
  // day = "MM-DD" или "сегодня/+N"
  if (day === "сегодня") return "сегодня";
  if (day.startsWith("+")) return day;
  return day;
}

export default function MarketPage() {
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [selected, setSelected] = useState<Artifact | null>(null);
  const [daily, setDaily] = useState<DailyPrice[]>([]);
  const [forecast, setForecast] = useState<number[] | null>(null);
  const [minPrice, setMinPrice] = useState<MarketPrice | null>(null);
  const [loadedFor, setLoadedFor] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [period, setPeriod] = useState<Period>("month");
  const [sparkData, setSparkData] = useState<Record<string, number[]>>({});

  useEffect(() => {
    api.artifacts().then((list) => {
      setArtifacts(list);
      // спарклайны: подгружаем 7-дневку для всех (пачками по 8, чтобы не долбить API)
      const ids = list.map((a) => a.item_id);
      const batches: string[][] = [];
      for (let i = 0; i < ids.length; i += 8) batches.push(ids.slice(i, i + 8));
      batches.forEach((batch, bi) => {
        setTimeout(() => {
          batch.forEach((id) => {
            const art = list.find((a) => a.item_id === id);
            const q = art?.min_quality ?? 0;
            const u = art?.upgrades?.length ? Math.min(...art.upgrades) : 0;
            api.dailyPrices(id, q, u, 7).then((rows) => {
              setSparkData((prev) => ({ ...prev, [id]: rows.map((r) => r.avg_price) }));
            }).catch(() => undefined);
          });
        }, bi * 300);
      });
    }).catch(() => undefined);
  }, []);

  const periodDays = PERIODS.find(([k]) => k === period)?.[2] ?? 30;

  useEffect(() => {
    if (!selected) return;
    let alive = true;
    const q = selected.min_quality ?? 0;
    const u = selected.upgrades.length ? Math.min(...selected.upgrades) : 0;
    api.dailyPrices(selected.item_id, q, u, periodDays).then((d) => alive && setDaily(d)).catch(() => alive && setDaily([]));
    api.forecast(selected.item_id, q, u).then((r) => alive && setForecast(r.forecast)).catch(() => alive && setForecast(null));
    api.marketPrice(selected.item_id, q, u)
      .then((m) => { if (!alive) return; setMinPrice(m); setError(null); setLoadedFor(selected.item_id); })
      .catch((e) => {
        if (!alive) return;
        setError(e instanceof Error && e.message.includes("404") ? "снапшот после цикла сканера" : (e instanceof Error ? e.message : "ошибка"));
        setLoadedFor(selected.item_id);
      });
    return () => { alive = false; };
  }, [selected, periodDays]);

  const loading = selected !== null && loadedFor !== selected.item_id;

  const sorted = useMemo(() => {
    const list = [...artifacts];
    list.sort((a, b) => {
      if (a.category !== b.category) return a.category === "liquid" ? -1 : 1;
      return a.name.localeCompare(b.name, "ru");
    });
    return list;
  }, [artifacts]);

  const filtered = sorted.filter((a) => a.name.toLowerCase().includes(query.toLowerCase()));

  const last = daily[daily.length - 1];
  const chartData = [
    ...daily.map((d) => ({ day: shortDay(d.day.slice(5)), price: d.avg_price })),
    ...(last ? [{ day: "сегодня", price: last.avg_price }] : []),
  ];
  const forecastData = (forecast ?? []).map((v, i) => ({
    day: i === 0 ? "завтра" : `+${i + 1} дн`,
    price: v,
    trend: last ? (v > last.avg_price ? "up" : v < last.avg_price ? "down" : "flat") : "flat",
  }));

  // сводка min/avg/max за период
  const summary = useMemo(() => {
    if (daily.length === 0) return null;
    const prices = daily.map((d) => d.avg_price);
    return {
      min: Math.min(...prices), avg: prices.reduce((s, p) => s + p, 0) / prices.length,
      max: Math.max(...prices),
    };
  }, [daily]);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1>Рынок</h1>
          <p className="sub">История цен и минимум по артефакту</p>
        </div>
        <input className="input w-56" placeholder="Поиск…" value={query} onChange={(e) => setQuery(e.target.value)} />
      </div>

      <div className="grid gap-4 lg:grid-cols-[300px_minmax(0,1fr)]">
        {/* Список артефактов: треугольник + спарклайн + имя */}
        <div className="card-flat min-w-0 overflow-hidden">
          <div className="max-h-[560px] overflow-y-auto">
            <table className="table">
              <tbody>
                {filtered.map((art) => {
                  const spark = sparkData[art.item_id] ?? [];
                  const change = spark.length >= 2
                    ? ((spark[spark.length - 1] - spark[spark.length - 2]) / spark[spark.length - 2]) * 100
                    : 0;
                  return (
                    <tr
                      key={art.item_id}
                      className={`row cursor-pointer ${selected?.item_id === art.item_id ? "row-active" : ""}`}
                      onClick={() => setSelected(art)}
                    >
                      <td className="w-6 pr-0"><TrendTriangle change={change} /></td>
                      <td className="w-16 pl-1 pr-0"><Sparkline points={spark} /></td>
                      <td className="truncate font-medium">{art.name}</td>
                    </tr>
                  );
                })}
                {filtered.length === 0 && <tr><td className="empty">Ничего не найдено</td></tr>}
              </tbody>
            </table>
          </div>
        </div>

        {/* Детали */}
        <div className="min-w-0 space-y-3">
          {selected && (
            <div className="flex flex-wrap items-center gap-x-5 gap-y-1 text-sm">
              <span className="text-muted">Минимум: <b className="text-text">{minPrice?.min_price != null ? minPrice.min_price.toLocaleString("ru-RU") + " руб." : "—"}</b></span>
              {summary && (
                <>
                  <span className="text-muted">мин: <b className="text-text">{summary.min.toLocaleString("ru-RU")}</b></span>
                  <span className="text-muted">ср: <b className="text-text">{Math.round(summary.avg).toLocaleString("ru-RU")}</b></span>
                  <span className="text-muted">макс: <b className="text-text">{summary.max.toLocaleString("ru-RU")}</b></span>
                </>
              )}
              {error && <span className="err">{error}</span>}
              <div className="ml-auto flex gap-1">
                {PERIODS.map(([key, label]) => (
                  <button
                    key={key}
                    className={`btn btn-sm ${period === key ? "btn-primary" : "btn-ghost"}`}
                    onClick={() => setPeriod(key)}
                  >{label}</button>
                ))}
              </div>
            </div>
          )}

          <div className="card-flat h-72 p-3">
            {loading ? (
              <div className="flex h-full items-center justify-center"><p className="sub">Загрузка…</p></div>
            ) : chartData.length > 1 ? (
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={chartData} margin={{ top: 8, right: 12, bottom: 0, left: 0 }}>
                  <CartesianGrid stroke="var(--line)" vertical={false} />
                  <XAxis dataKey="day" tick={{ fontSize: 11, fill: "var(--muted)" }} axisLine={{ stroke: "var(--line)" }} tickLine={false} />
                  <YAxis tick={{ fontSize: 11, fill: "var(--muted)" }} width={72} axisLine={false} tickLine={false} />
                  <Tooltip
                    contentStyle={{ background: "var(--raised)", border: "1px solid var(--line-strong)", borderRadius: 4, fontSize: 12 }}
                    labelStyle={{ color: "var(--muted)" }}
                    formatter={(v: number) => [`${v.toLocaleString("ru-RU")} руб.`, "цена"]}
                  />
                  <Line type="monotone" dataKey="price" stroke="var(--accent)" strokeWidth={1.5} dot={false} />
                </LineChart>
              </ResponsiveContainer>
            ) : (
              <div className="flex h-full flex-col items-center justify-center gap-2">
                <p className="sub">Недостаточно данных</p>
                <p className="mono">история появится после цикла синка (~15 мин)</p>
              </div>
            )}
          </div>

          {/* Прогноз отдельным блоком */}
          {forecastData.length > 0 && (
            <div className="card-flat p-3">
              <p className="lbl mb-2">Прогноз на 3 дня</p>
              <div className="forecast-row">
                {forecastData.map((f, i) => (
                  <div key={i} className="forecast-cell">
                    <div className="f-day">{f.day}</div>
                    <div className="f-val">{Math.round(f.price).toLocaleString("ru-RU")}</div>
                    <div className="f-trend">
                      {f.trend === "up" ? <span className="trend trend-up">▲</span> :
                       f.trend === "down" ? <span className="trend trend-down">▼</span> :
                       <span className="trend trend-flat">■</span>}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
