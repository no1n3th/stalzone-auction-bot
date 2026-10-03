import { useEffect, useState } from "react";
import { api } from "../api";
import { useAuth } from "../auth/AuthContext";

interface Settings {
  sound_enabled: boolean;
  sound_volume: number;
  sound_events: string[];
  profit_threshold_pct: number;
  theme: string;
}

const EVENT_OPTIONS: [string, string][] = [
  ["lot", "Выгодный лот"], ["mid_lot", "Средняя заточка"], ["dump", "Дамп цены"],
  ["meta_enter", "Вход в мету"], ["meta_exit", "Выход из меты"],
  ["season_start", "Начало сезона"], ["season_end", "Конец сезона"],
  ["seasonal_drop", "Сезонное падение"], ["season_dip_buy", "Dip-buy"],
];

export default function SettingsPage() {
  const { user } = useAuth();
  const [s, setS] = useState<Settings | null>(null);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    api.getSettings().then(setS).catch(() => undefined);
  }, []);

  function update(patch: Partial<Settings>) {
    if (!s) return;
    const next = { ...s, ...patch };
    setS(next);
    api.putSettings(patch).then(() => { setSaved(true); setTimeout(() => setSaved(false), 1500); }).catch(() => undefined);
  }

  if (!user) return <p className="empty">Войдите, чтобы открыть настройки.</p>;
  if (!s) return <div className="space-y-3">{[...Array(3)].map((_, i) => <div key={i} className="skeleton h-10" />)}</div>;

  return (
    <div className="space-y-5 max-w-lg">
      <div className="flex items-center gap-3">
        <h1>Настройки</h1>
        {saved && <span className="badge">Сохранено</span>}
      </div>

      <div className="card space-y-4">
        <h2>Уведомления и звук</h2>
        <p className="sub">Звук играет только когда вкладка не активна (вы в игре или другом окне).</p>

        <label className="flex items-center gap-3">
          <input type="checkbox" checked={s.sound_enabled} onChange={(e) => update({ sound_enabled: e.target.checked })} />
          <span>Включить звук уведомлений</span>
        </label>

        <div className="space-y-1">
          <div className="flex items-center justify-between">
            <span className="text-sm">Громкость</span>
            <span className="mono">{Math.round(s.sound_volume * 100)}%</span>
          </div>
          <input
            type="range" min={0} max={100} value={Math.round(s.sound_volume * 100)}
            onChange={(e) => update({ sound_volume: Number(e.target.value) / 100 })}
            className="w-full"
          />
        </div>

        <div className="space-y-2">
          <p className="text-sm font-medium">Звук для событий:</p>
          {EVENT_OPTIONS.map(([key, label]) => (
            <label key={key} className="flex items-center gap-3">
              <input
                type="checkbox"
                checked={s.sound_events.includes(key)}
                onChange={(e) => {
                  const next = e.target.checked
                    ? [...s.sound_events, key]
                    : s.sound_events.filter((k) => k !== key);
                  update({ sound_events: next });
                }}
              />
              <span>{label}</span>
            </label>
          ))}
        </div>

        <div className="space-y-1">
          <div className="flex items-center justify-between">
            <span className="text-sm">Звук только если профит выше, %</span>
            <span className="mono">{s.profit_threshold_pct}%</span>
          </div>
          <input
            type="range" min={1} max={50} step={1} value={s.profit_threshold_pct}
            onChange={(e) => update({ profit_threshold_pct: Number(e.target.value) })}
            className="w-full"
          />
          <p className="sub">Например, 10% — звук только для лотов с профитом 10% и выше.</p>
        </div>
      </div>

      <div className="card space-y-3">
        <h2>Внешний вид</h2>
        <div className="flex gap-2">
          <button className={`btn ${s.theme === "dark" ? "btn-primary" : ""}`} onClick={() => update({ theme: "dark" })}>Тёмная</button>
          <button className={`btn ${s.theme === "light" ? "btn-primary" : ""}`} onClick={() => update({ theme: "light" })}>Светлая</button>
        </div>
      </div>
    </div>
  );
}
