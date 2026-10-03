import { useEffect, useState } from "react";
import { api, type Digest } from "../api";

export default function DashboardPage() {
  const [digest, setDigest] = useState<Digest | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.digest().then(setDigest).catch((e) => setError(e instanceof Error ? e.message : "ошибка"));
  }, []);

  return (
    <div className="space-y-4">
      <div>
        <h1>Обзор</h1>
        <p className="sub">Сводка продаж за 24 часа</p>
      </div>
      {error && <p className="err">{error}</p>}
      <div className="card-flat overflow-x-auto p-0">
        <table className="table">
          <thead>
            <tr><th>Предмет</th><th className="num">Продаж</th><th className="num">Средняя</th><th className="num">Мин</th><th className="num">Макс</th></tr>
          </thead>
          <tbody>
            {(digest?.items ?? []).map((it) => (
              <tr key={it.item_id} className="row">
                <td className="font-medium">{it.item_id}</td>
                <td className="num">{it.sales}</td>
                <td className="num">{it.avg_price != null ? it.avg_price.toLocaleString("ru-RU") : "—"}</td>
                <td className="num">{it.min_price != null ? it.min_price.toLocaleString("ru-RU") : "—"}</td>
                <td className="num">{it.max_price != null ? it.max_price.toLocaleString("ru-RU") : "—"}</td>
              </tr>
            ))}
            {digest && digest.items.length === 0 && <tr><td colSpan={5} className="empty">Продаж за сутки ещё нет</td></tr>}
            {!digest && !error && <tr><td colSpan={5} className="empty"><span className="skeleton inline-block h-4 w-full" /></td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  );
}
