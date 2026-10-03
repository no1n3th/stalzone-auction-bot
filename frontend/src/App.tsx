import DealsPage, { DEALS_HINT } from "./pages/DealsPage";
import { Navigate, Route, Routes } from "react-router-dom";
import { AuthProvider, useAuth } from "./auth/AuthContext";
import { RequireAuth } from "./auth/AuthGate";
import Layout from "./components/Layout";
import AdminPage from "./pages/AdminPage";
import AuthPage from "./pages/AuthPage";
import FeedPage from "./pages/FeedPage";
import MarketPage from "./pages/MarketPage";
import SettingsPage from "./pages/SettingsPage";

const PRIVATE_HINT = "Войдите или зарегистрируйтесь, чтобы открыть этот раздел.";

function AdminRoute() {
  const { user } = useAuth();
  return user?.is_admin ? <AdminPage /> : <Navigate to="/" replace />;
}

function AppRoutes() {
  const { loading } = useAuth();
  if (loading) {
    return (
      <div className="flex h-full items-center justify-center">
        <div className="flex items-center gap-3 text-[var(--muted)]">
          <span className="live-dot" />
          <span>Загрузка…</span>
        </div>
      </div>
    );
  }
  return (
    <Routes>
      <Route path="auth/:pane?" element={<AuthPage />} />
      <Route element={<Layout />}>
        {/* Главная — лента событий, открыта всем */}
        <Route index element={<FeedPage />} />
        <Route path="market" element={<MarketPage />} />
        <Route
          path="settings"
          element={
            <RequireAuth title="Настройки доступны только авторизованным" hint={PRIVATE_HINT}>
              <SettingsPage />
            </RequireAuth>
          }
        />
        <Route
          path="deals"
          element={
            <RequireAuth title="Сделки доступны только авторизованным" hint={DEALS_HINT}>
              <DealsPage />
            </RequireAuth>
          }
        />
        <Route path="admin" element={<AdminRoute />} />
        <Route path="feed" element={<Navigate to="/" replace />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}

export default function App() {
  return (
    <AuthProvider>
      <div className="weave" />
      <AppRoutes />
    </AuthProvider>
  );
}
