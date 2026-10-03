/** Typed API client with cookie auth. */

export interface User {
  username: string;
  is_admin: boolean;
}

export interface Deal {
  id: number;
  item_id: string;
  rarity: number;
  upgrade: number;
  amount: number;
  buy_price: number;
  sell_price: number | null;
  status: string;
  note: string;
  created_at: string;
  closed_at: string | null;
  version: number;
  net_profit: number | null;
  roi_pct: number | null;
}

export interface DealsSummary {
  realized_profit: number;
  roi_pct: number;
  invested_open: number;
  balance: number;
  projected_balance: number;
  open_count: number;
  closed_count: number;
}

export interface Artifact {
  item_id: string;
  name: string;
  category: string;
  min_quality: number | null;
  upgrades: number[];
  profit_threshold_pct: number | null;
}

export interface Notification {
  id: number;
  event_type: string;
  item_id: string;
  payload: Record<string, unknown>;
  created_at: string;
}

export interface DailyPrice {
  day: string;
  avg_price: number;
  volume: number;
}

export interface ItemSearchResult {
  item_id: string;
  name: string;
}

export interface MarketPrice {
  item_id: string;
  quality: number;
  upgrade: number;
  min_price: number | null;
}

export interface ForecastResponse {
  item_id: string;
  forecast: number[];
}

export interface DigestItem {
  item_id: string;
  sales: number;
  avg_price: number | null;
  min_price: number | null;
  max_price: number | null;
}

export interface Digest {
  since: string;
  items: DigestItem[];
}

export interface Meta {
  version: string;
  fee_pct: number;
  registration_open: boolean;
}

export interface AdminStats {
  history_rows: number;
  history_items: number;
  dlq_pending: number;
  users: number;
  deals: number;
  ws_clients: number;
  metrics: Record<string, number>;
}

export interface AuditEntry {
  id: number;
  username: string;
  action: string;
  details: string;
  created_at: string | null;
}

export class ApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

/** Fired when a protected call answers 401 (session expired) so the UI can drop the user. */
export const UNAUTHORIZED_EVENT = "sz:unauthorized";

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const { headers, ...rest } = options;
  const resp = await fetch(path, {
    credentials: "same-origin",
    ...rest,
    headers: { "Content-Type": "application/json", ...headers },
  });
  if (!resp.ok) {
    if (resp.status === 401 && !path.startsWith("/api/auth/")) {
      window.dispatchEvent(new Event(UNAUTHORIZED_EVENT));
    }
    let detail: unknown = resp.statusText;
    try {
      const body = await resp.json();
      detail = body.detail ?? detail;
    } catch {
      /* non-json error */
    }
    throw new ApiError(resp.status, detailToText(detail));
  }
  if (resp.status === 204) {
    return undefined as T;
  }
  return (await resp.json()) as T;
}

export const api = {
  // auth
  register: (username: string, password: string) =>
    request<User>("/api/auth/register", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    }),
  login: (username: string, password: string) =>
    request<User>("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    }),
  logout: () => request<void>("/api/auth/logout", { method: "POST" }),
  me: () => request<User>("/api/auth/me"),
  meta: () => request<Meta>("/api/meta"),

  // deals
  listDeals: (params: { status?: string; item_id?: string } = {}) => {
    const qs = new URLSearchParams();
    if (params.status) qs.set("status", params.status);
    if (params.item_id) qs.set("item_id", params.item_id);
    const suffix = qs.toString() ? `?${qs}` : "";
    return request<Deal[]>(`/api/deals${suffix}`);
  },
  createDeal: (deal: {
    item_id: string;
    rarity: number;
    upgrade: number;
    amount: number;
    buy_price: number;
    note?: string;
  }) => request<Deal>("/api/deals", { method: "POST", body: JSON.stringify(deal) }),
  updateDeal: (id: number, version: number, fields: DealPatch) =>
    request<Deal>(`/api/deals/${id}`, {
      method: "PATCH",
      body: JSON.stringify({ version, ...fields }),
    }),
  closeDeal: (id: number, version: number, sellPrice: number) =>
    request<Deal>(`/api/deals/${id}/close`, {
      method: "POST",
      body: JSON.stringify({ version, sell_price: sellPrice }),
    }),
  deleteDeal: (id: number) => request<void>(`/api/deals/${id}`, { method: "DELETE" }),
  dealsSummary: () => request<DealsSummary>("/api/deals/summary"),

  // market
  artifacts: () => request<Artifact[]>("/api/artifacts"),
  marketPrice: (itemId: string, quality = 0, upgrade = 0) =>
    request<MarketPrice>(`/api/market/${itemId}?quality=${quality}&upgrade=${upgrade}`),
  dailyPrices: (itemId: string, quality = 0, upgrade = 0, days?: number) =>
    request<DailyPrice[]>(`/api/daily-prices/${itemId}?quality=${quality}&upgrade=${upgrade}${days ? `&days=${days}` : ""}`),
  forecast: (itemId: string, quality = 0, upgrade = 0) =>
    request<ForecastResponse>(`/api/forecast/${itemId}?quality=${quality}&upgrade=${upgrade}`),
  digest: () => request<Digest>("/api/digest"),
  searchItems: (q: string) => request<ItemSearchResult[]>(`/api/items?q=${encodeURIComponent(q)}`),
  notifications: (limit = 50, eventType?: string) =>
    request<Notification[]>(
      `/api/notifications?limit=${limit}${eventType ? `&event_type=${eventType}` : ""}`,
    ),

  // admin
  adminStats: () => request<AdminStats>("/api/admin/stats"),
  replayDlq: () => request<{ sent: number; failed: number }>("/api/admin/replay-dlq", {
    method: "POST",
  }),
  adminAudit: () => request<AuditEntry[]>("/api/admin/audit"),
  getFlags: () => request<Record<string, boolean>>("/api/admin/flags"),
  setFlag: (name: string, enabled: boolean) =>
    request<{ name: string; enabled: boolean }>(`/api/admin/flags/${name}`, {
      method: "POST",
      body: JSON.stringify({ enabled }),
    }),
  getConfig: () => request<Record<string, string>>("/api/admin/config"),
  setConfig: (key: string, value: string) =>
    request<{ key: string; value: string }>(`/api/admin/config/${key}`, {
      method: "POST",
      body: JSON.stringify({ value }),
    }),
  getSettings: () =>
    request<{
      sound_enabled: boolean; sound_volume: number; sound_events: string[];
      profit_threshold_pct: number; theme: string;
    }>("/api/auth/me/settings"),
  putSettings: (patch: Partial<{
    sound_enabled: boolean; sound_volume: number; sound_events: string[];
    profit_threshold_pct: number; theme: string;
  }>) => request<Record<string, unknown>>("/api/auth/me/settings", { method: "PUT", body: JSON.stringify(patch) }),
  listUsers: () => request<User[]>("/api/auth/users"),
  deleteUser: (username: string) =>
    request<void>(`/api/auth/users/${encodeURIComponent(username)}`, { method: "DELETE" }),
};

export type DealPatch = Partial<
  Pick<Deal, "item_id" | "rarity" | "upgrade" | "amount" | "buy_price" | "note">
>;

export function detailToText(d: unknown): string {
  if (typeof d === "string") return d;
  if (Array.isArray(d))
    return d.map((e) => `${(e?.loc ?? []).slice(1).join(".")}: ${e?.msg ?? ""}`.trim()).join("; ");
  return "Ошибка запроса";
}
