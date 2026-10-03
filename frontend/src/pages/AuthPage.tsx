import { type FormEvent, useEffect, useState } from "react";
import { Link, useLocation, useNavigate, useParams } from "react-router-dom";
import { api } from "../api";
import { useAuth } from "../auth/AuthContext";
import { validateRegistration } from "../auth/validation";

export default function AuthPage() {
  const params = useParams();
  const location = useLocation();
  const navigate = useNavigate();
  const { login, register } = useAuth();
  const [meta, setMeta] = useState<{ registration_open: boolean } | null>(null);
  const [isLogin, setIsLogin] = useState(params.pane !== "register");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [repeat, setRepeat] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api.meta().then(setMeta).catch(() => setMeta(null));
  }, []);

  const from = (location.state as { from?: string } | null)?.from ?? "/deals";

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    if (!isLogin) {
      const validationError = validateRegistration(username, password, repeat);
      if (validationError) return setError(validationError);
    }
    setBusy(true);
    try {
      if (isLogin) {
        await login(username, password);
      } else {
        await register(username, password);
        // после регистрации — на форму входа (не авто-логин)
        setIsLogin(true);
        setPassword("");
        setRepeat("");
        setError(null);
        return;
      }
      navigate(from, { replace: true });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Ошибка авторизации");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="auth-shell">
      <Link to="/" className="back">
        На главную
      </Link>
      <div className="auth-card anim-card">
        <h1 className="text-lg font-semibold">{isLogin ? "Вход" : "Регистрация"}</h1>
        <p className="sub">
          {isLogin
            ? "Войдите, чтобы открыть сделки и ленту."
            : "Создайте аккаунт — первый пользователь становится администратором."}
        </p>
        <form className="space-y-3" onSubmit={handleSubmit}>
          <input
            className="input"
            placeholder="Логин (a-z, 0-9, _, 3–20)"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoComplete="username"
          />
          <input
            className="input"
            type="password"
            placeholder="Пароль (минимум 6 символов)"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete={isLogin ? "current-password" : "new-password"}
          />
          {!isLogin && (
            <input
              className="input"
              type="password"
              placeholder="Повторите пароль"
              value={repeat}
              onChange={(e) => setRepeat(e.target.value)}
              autoComplete="new-password"
            />
          )}
          {error && <p className="err">{error}</p>}
          <button className="btn btn-primary w-full" type="submit" disabled={busy}>
            {busy ? "…" : isLogin ? "Войти" : "Зарегистрироваться"}
          </button>
        </form>
        <div className="foot !border-0 !p-0">
          <span />
          <button
            type="button"
            className="text-gold hover:underline"
            onClick={() => {
              setIsLogin((v) => !v);
              setError(null);
            }}
          >
            {isLogin ? "Создать аккаунт" : "Уже есть аккаунт? Войти"}
          </button>
        </div>
        {isLogin && (
          <p className="text-xs text-ink/40">Сброс пароля выполняет администратор.</p>
        )}
        {!isLogin && meta && !meta.registration_open && (
          <p className="text-xs text-danger">Регистрация закрыта.</p>
        )}
      </div>
    </div>
  );
}
