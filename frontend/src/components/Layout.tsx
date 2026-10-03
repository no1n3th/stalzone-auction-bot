import { useEffect, useState } from "react";
import { Link, NavLink, Outlet } from "react-router-dom";
import { api, type Meta } from "../api";
import { useAuth } from "../auth/AuthContext";

const TABS = [
  { to: "/", label: "Лента", end: true },
  { to: "/market", label: "Рынок", end: false },
  { to: "/deals", label: "Сделки", end: false },
];

export default function Layout() {
  const { user, logout } = useAuth();
  const [meta, setMeta] = useState<Meta | null>(null);
  const [theme, setTheme] = useState<string>("dark");

  useEffect(() => {
    api.meta().then(setMeta).catch(() => setMeta(null));
    api.getSettings().then((s) => {
      setTheme(s.theme);
      document.documentElement.setAttribute("data-theme", s.theme);
    }).catch(() => undefined);
  }, []);

  function toggleTheme() {
    const next = theme === "dark" ? "light" : "dark";
    setTheme(next);
    document.documentElement.setAttribute("data-theme", next);
    api.putSettings({ theme: next }).catch(() => undefined);
  }

  return (
    <div className="flex h-full flex-col">
      <header className="flex h-12 items-center gap-4 border-b border-[var(--line)] bg-[var(--bg)] px-4">
        <Link to="/" className="brand">Stalzone</Link>
        <nav className="flex items-center gap-1" aria-label="Основная навигация">
          {TABS.map((t) => (
            <NavLink key={t.to} to={t.to} end={t.end} className={({ isActive }) => `navlink${isActive ? " is-active" : ""}`}>
              {t.label}
            </NavLink>
          ))}
          {user?.is_admin && (
            <NavLink to="/admin" className={({ isActive }) => `navlink${isActive ? " is-active" : ""}`}>Админка</NavLink>
          )}
        </nav>
        <div className="ml-auto flex items-center gap-3 text-sm">
          <button type="button" className="btn btn-ghost btn-sm" onClick={toggleTheme} title="Переключить тему">
            {theme === "dark" ? "☾" : "☀"}
          </button>
          {user ? (
            <>
              <Link to="/settings" className="text-[var(--muted)] hover:text-[var(--text)]">{user.username}</Link>
              <button type="button" className="btn btn-ghost btn-sm" onClick={logout}>Выйти</button>
            </>
          ) : (
            <Link to="/auth/login" className="btn btn-primary btn-sm">Войти</Link>
          )}
        </div>
      </header>

      <main className="flex-1 overflow-y-auto p-4 md:p-6">
        <div className="mx-auto w-full max-w-6xl"><Outlet /></div>
      </main>

      <footer className="foot">
        {meta ? `комиссия ${meta.fee_pct}% — v${meta.version}` : "Stalzone"}
      </footer>
    </div>
  );
}
