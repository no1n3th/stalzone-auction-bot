import { type FormEvent, useEffect, useMemo, useState } from "react";
import { api, type Deal, type DealsSummary, type ItemSearchResult } from "../api";
import { useAuth } from "../auth/AuthContext";

export const DEALS_HINT =
  "Сделки — журнал трейдера. Заносите покупки, отмечайте продажи, следите за прибылью.";

type SortKey = "created_at" | "item_id" | "buy_price" | "status" | "amount" | "upgrade";

interface Form { item_id: string; rarity: number; upgrade: number; amount: number; buy_price: string; note: string; }
const INITIAL: Form = { item_id: "", rarity: 4, upgrade: 0, amount: 1, buy_price: "", note: "" };

const parsePrice = (s: string): number | null => {
  const v = Number(s.replace(",", ".").replace(/\s/g, ""));
  return Number.isFinite(v) && v > 0 ? v : null;
};

export default function DealsPage() {
  const { user } = useAuth();
  const [deals, setDeals] = useState<Deal[]>([]);
  const [summary, setSummary] = useState<DealsSummary | null>(null);
  const [statusFilter, setStatusFilter] = useState("");
  const [sortKey, setSortKey] = useState<SortKey>("created_at");
  const [sortDir, setSortDir] = useState<1 | -1>(-1);
  const [form, setForm] = useState<Form>(INITIAL);
  const [formError, setFormError] = useState<string | null>(null);
  const [hints, setHints] = useState<ItemSearchResult[]>([]);

  async function reload() {
    const [d, s] = await Promise.all([api.listDeals({ status: statusFilter || undefined }), api.dealsSummary()]);
    setDeals(d); setSummary(s);
  }
  useEffect(() => { let c = false; api.listDeals({ status: statusFilter || undefined }).then((d) => !c && setDeals(d)).catch(() => undefined); return () => { c = true; }; }, [statusFilter]);
  useEffect(() => { let c = false; api.dealsSummary().then((s) => !c && setSummary(s)).catch(() => undefined); return () => { c = true; }; }, []);
  useEffect(() => {
    const q = form.item_id.trim();
    if (q.length < 2) return;
    let c = false;
    api.searchItems(q).then((r) => !c && setHints(r.slice(0, 8))).catch(() => undefined);
    return () => { c = true; };
  }, [form.item_id]);

  const visible = useMemo(() => [...deals].sort((a, b) => {
    if (sortKey === "created_at") return sortDir * (a.created_at < b.created_at ? -1 : 1);
    if (sortKey === "item_id") return sortDir * a.item_id.localeCompare(b.item_id);
    if (sortKey === "status") return sortDir * a.status.localeCompare(b.status);
    return sortDir * (a[sortKey] - b[sortKey]);
  }), [deals, sortKey, sortDir]);

  function toggle(k: SortKey) {
    if (k === sortKey) {
      setSortDir((d) => (d === 1 ? -1 : 1));
    } else {
      setSortKey(k);
      setSortDir(1);
    }
  }

  async function create(e: FormEvent) {
    e.preventDefault(); setFormError(null);
    const price = parsePrice(form.buy_price);
    if (!form.item_id.trim()) return setFormError("Укажите предмет.");
    if (price == null) return setFormError("Цена — положительное число.");
    try {
      await api.createDeal({ item_id: form.item_id.trim().toLowerCase(), rarity: form.rarity, upgrade: form.upgrade, amount: form.amount, buy_price: price, note: form.note });
      setForm(INITIAL); await reload();
    } catch (err) { setFormError(err instanceof Error ? err.message : "Ошибка"); }
  }
  async function close(deal: Deal) {
    const raw = window.prompt(`Цена продажи для ${deal.item_id} +${deal.upgrade}?`, "");
    if (raw == null) return;
    const price = parsePrice(raw); if (price == null) return;
    try { await api.closeDeal(deal.id, deal.version, price); await reload(); } catch (e) { window.alert(e instanceof Error ? e.message : "Ошибка"); }
  }
  async function remove(deal: Deal) {
    if (!window.confirm(`Удалить сделку #${deal.id}?`)) return;
    try { await api.deleteDeal(deal.id); await reload(); } catch (e) { window.alert(e instanceof Error ? e.message : "Ошибка"); }
  }

  if (!user) return null;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div><h1>Сделки</h1><p className="sub">Журнал трейдера — только для вас.</p></div>
        <select className="input w-36" value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
          <option value="">Все</option><option value="open">Открытые</option><option value="closed">Закрытые</option>
        </select>
      </div>

      {summary && (
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
          <div className="card py-2"><div className="lbl">Профит</div><div className={`text-lg font-semibold ${summary.realized_profit >= 0 ? "text-[var(--profit)]" : "text-[var(--loss)]"}`}>{summary.realized_profit.toLocaleString("ru-RU", { maximumFractionDigits: 0 })}</div></div>
          <div className="card py-2"><div className="lbl">Баланс</div><div className={`text-lg font-semibold ${summary.balance >= 0 ? "text-[var(--profit)]" : "text-[var(--loss)]"}`}>{summary.balance.toLocaleString("ru-RU", { maximumFractionDigits: 0 })}</div></div>
          <div className="card py-2"><div className="lbl">Вложено</div><div className="text-lg font-semibold">{summary.invested_open.toLocaleString("ru-RU", { maximumFractionDigits: 0 })}</div></div>
          <div className="card py-2"><div className="lbl">Прогноз баланса</div><div className="text-lg font-semibold">{summary.projected_balance.toLocaleString("ru-RU", { maximumFractionDigits: 0 })}</div></div>
        </div>
      )}

      <form className="card space-y-2" onSubmit={create}>
        <h2>Новая сделка</h2>
        <div className="grid gap-2 md:grid-cols-6">
          <div className="relative md:col-span-2">
            <input className="input" placeholder="Предмет (название)" value={form.item_id} onChange={(e) => setForm({ ...form, item_id: e.target.value })} />
            {form.item_id.trim().length >= 2 && hints.length > 0 && (
              <ul className="absolute z-10 mt-1 w-full card-flat py-1">
                {hints.map((h) => (
                  <li key={h.item_id}><button type="button" className="row block w-full px-3 py-1.5 text-left text-sm" onClick={() => { setForm({ ...form, item_id: h.item_id }); setHints([]); }}>{h.name}</button></li>
                ))}
              </ul>
            )}
          </div>
          <select className="input" value={form.rarity} onChange={(e) => setForm({ ...form, rarity: Number(e.target.value) })}>
            {[0,1,2,3,4,5].map((q) => <option key={q} value={q}>Качество {q}</option>)}
          </select>
          <input className="input" type="number" min={0} max={15} value={form.upgrade} onChange={(e) => setForm({ ...form, upgrade: Number(e.target.value) })} placeholder="+0" />
          <input className="input" type="number" min={1} value={form.amount} onChange={(e) => setForm({ ...form, amount: Number(e.target.value) })} placeholder="Кол-во" />
          <input className="input" value={form.buy_price} onChange={(e) => setForm({ ...form, buy_price: e.target.value })} placeholder="Цена покупки" />
        </div>
        <div className="flex gap-2">
          <input className="input flex-1" value={form.note} onChange={(e) => setForm({ ...form, note: e.target.value })} placeholder="Заметка" />
          <button className="btn btn-primary" type="submit">Добавить</button>
        </div>
        {formError && <p className="err">{formError}</p>}
      </form>

      <div className="card-flat overflow-x-auto p-0">
        <table className="table">
          <thead>
            <tr>
              {([["created_at","Дата"],["item_id","Предмет"],["upgrade","+"],["amount","Кол-во"],["buy_price","Покупка"],["status","Статус"]] as [SortKey,string][]).map(([k,l]) => (
                <th key={k} className="cursor-pointer" onClick={() => toggle(k)}>{l}{sortKey===k?(sortDir===1?" ↑":" ↓"):""}</th>
              ))}
              <th className="text-right">Действия</th>
            </tr>
          </thead>
          <tbody>
            {visible.map((d) => (
              <tr key={d.id} className="row">
                <td className="text-muted whitespace-nowrap">{d.created_at.slice(0,10)}</td>
                <td className="font-medium">{d.item_id}</td>
                <td className="num">+{d.upgrade}</td>
                <td className="num">{d.amount}</td>
                <td className="num">{d.buy_price.toLocaleString("ru-RU")}</td>
                <td>{d.status === "open" ? <span className="badge">открыта</span> : <span className="badge badge-profit">закрыта {d.net_profit != null && `(${d.net_profit >= 0 ? "+" : ""}${d.net_profit})`}</span>}</td>
                <td className="text-right whitespace-nowrap">
                  {d.status === "open" && <button className="btn btn-ghost btn-sm" onClick={() => close(d)}>Закрыть</button>}
                  <button className="btn btn-danger btn-sm" onClick={() => remove(d)}>Удалить</button>
                </td>
              </tr>
            ))}
            {visible.length === 0 && <tr><td colSpan={7} className="empty">Сделок нет — создайте первую.</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  );
}
