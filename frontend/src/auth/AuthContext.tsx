import { createContext, type ReactNode, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { api, UNAUTHORIZED_EVENT, type User } from "../api";

interface AuthState {
  user: User | null;
  loading: boolean;
  login: (username: string, password: string) => Promise<void>;
  register: (username: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthState | null>(null);

/** Single source of truth for the current user (cookie session on the server). */
export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    api
      .me()
      .then((u) => alive && setUser(u))
      .catch(() => alive && setUser(null))
      .finally(() => alive && setLoading(false));
    const onExpired = () => setUser(null);
    window.addEventListener(UNAUTHORIZED_EVENT, onExpired);
    return () => {
      alive = false;
      window.removeEventListener(UNAUTHORIZED_EVENT, onExpired);
    };
  }, []);

  const login = useCallback(async (username: string, password: string) => {
    setUser(await api.login(username, password));
  }, []);
  const register = useCallback(async (username: string, password: string) => {
    setUser(await api.register(username, password)); // server starts the session right away
  }, []);
  const logout = useCallback(async () => {
    try {
      await api.logout();
    } finally {
      setUser(null);
    }
  }, []);

  const value = useMemo(
    () => ({ user, loading, login, register, logout }),
    [user, loading, login, register, logout],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside <AuthProvider>");
  return ctx;
}
