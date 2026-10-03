import { useEffect, useState } from "react";
import { api, type AdminStats, type AuditEntry, type User } from "../api";

const HUMAN_FLAGS: [string, string, string][] = [
  ["scanner_enabled", "Сканирование аукциона", "Ищет выгодные лоты каждую минуту. Выключите, чтобы паузить поиск."],
  ["history_sync_enabled", "Сбор истории продаж", "Скачивает историю продаж для графиков и аналитики (раз в 15 мин)."],
  ["meta_monitor_enabled", "Аналитика рынка", "Следит за дампами цен, входом/выходом из меты."],
  ["season_monitor_enabled", "Сезонные события", "Оповещает о начале/конце сезонов и сезонных падениях."],
  ["charts_enabled", "Графики в Discord", "Рисует PNG-графики и прикрепляет к уведомлениям."],
  ["discord_enabled", "Уведомления в Discord", "Отправляет все алерты в ваш Discord-канал."],
];

export default function AdminPage() {
  const [stats, setStats] = useState<AdminStats | null>(null);
  const [flags, setFlags] = useState<Record<string, boolean>>({});
  const [users, setUsers] = useState<User[]>([]);
  const [audit, setAudit] = useState<AuditEntry[]>([]);
  const [message, setMessage] = useState<string | null>(null);

  function reload() {
    api.adminStats().then(setStats).catch(() => setStats(null));
    api.getFlags().then(setFlags).catch(() => setFlags({}));
    api.listUsers().then(setUsers).catch(() => setUsers([]));
    api.adminAudit().then(setAudit).catch(() => setAudit([]));
  }
  useEffect(reload, []);

  async function flash(fn: () => Promise<unknown>, ok: string) {
    try { const r = await fn(); setMessage(typeof r === "string" && r ? r : ok); reload(); }
    catch (e) { setMessage(e instanceof Error ? e.message : "Ошибка"); }
    window.setTimeout(() => setMessage(null), 3000);
  }

  return (
    <div className="space-y-5">
      <div className="flex items-center gap-3">
        <h1>Администрирование</h1>
        {message && <span className="badge">{message}</span>}
      </div>

      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 lg:grid-cols-6">
        {([
          ["Продаж в базе", stats?.history_rows], ["Предметов отслеживается", stats?.history_items],
          ["Ошибок отправки в Discord", stats?.dlq_pending], ["Пользователей", stats?.users],
          ["Сделок", stats?.deals], ["Онлайн (WebSocket)", stats?.ws_clients],
        ] as const).map(([l, v]) => (
          <div key={l} className="card py-2">
            <div className="lbl">{l}</div>
            <div className="mt-0.5 text-lg font-semibold">{v ?? "—"}</div>
          </div>
        ))}
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <div className="card-flat">
          <div className="border-b border-line px-4 py-2"><h2>Управление функциями</h2></div>
          <table className="table">
            <tbody>
              {HUMAN_FLAGS.map(([name, label, desc]) => (
                <tr key={name} className="row">
                  <td>
                    <div className="font-medium">{label}</div>
                    <div className="text-xs text-muted mt-0.5">{desc}</div>
                  </td>
                  <td className="text-right w-20">
                    <button
                      role="switch" aria-checked={flags[name] ?? true} aria-label={label}
                      onClick={() => flash(() => api.setFlag(name, !(flags[name] ?? true)), label)}
                      className={`relative h-5 w-9 rounded-full transition-colors ${(flags[name] ?? true) ? "bg-[var(--accent)]" : "bg-[var(--line-strong)]"}`}
                    >
                      <span className={`absolute top-0.5 h-4 w-4 rounded-full bg-white transition-transform ${(flags[name] ?? true) ? "translate-x-[18px]" : "translate-x-0.5"}`} />
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div className="space-y-4">
          <div className="card-flat">
            <div className="flex items-center justify-between border-b border-line px-4 py-2">
              <h2>Очередь уведомлений (DLQ)</h2>
              <button className="btn btn-sm" onClick={() => flash(async () => {
                const r = await api.replayDlq();
                return `Отправлено: ${r.sent}, ошибок: ${r.failed}`;
              }, "ok")}>Повторить отправку</button>
            </div>
            <p className="px-4 py-3 text-sm text-muted">
              Недоставленных: <b className={stats && stats.dlq_pending > 0 ? "text-[var(--loss)]" : "text-text"}>{stats?.dlq_pending ?? "—"}</b>
              <span className="block text-xs mt-1">Сообщения, которые не дошли до Discord. Нажмите «Повторить», чтобы отправить снова.</span>
            </p>
          </div>

          <div className="card-flat">
            <div className="border-b border-line px-4 py-2"><h2>Пользователи</h2></div>
            <table className="table">
              <tbody>
                {users.map((u) => (
                  <tr key={u.username} className="row">
                    <td className="font-medium">{u.username}</td>
                    <td>{u.is_admin && <span className="badge">администратор</span>}</td>
                    <td className="text-right">
                      <button className="btn btn-danger btn-sm" onClick={() => window.confirm(`Удалить пользователя ${u.username}?`) && flash(() => api.deleteUser(u.username), "ok")}>Удалить</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </div>

      <div className="card-flat">
        <div className="border-b border-line px-4 py-2"><h2>Журнал действий</h2></div>
        <div className="max-h-48 overflow-y-auto">
          <table className="table">
            <tbody>
              {audit.map((a) => (
                <tr key={a.id} className="row">
                  <td className="mono whitespace-nowrap">{a.created_at?.slice(0, 16).replace("T", " ")}</td>
                  <td>{a.username}</td>
                  <td>{a.action}</td>
                  <td className="text-muted">{a.details}</td>
                </tr>
              ))}
              {audit.length === 0 && <tr><td className="empty">Пока нет записей</td></tr>}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
