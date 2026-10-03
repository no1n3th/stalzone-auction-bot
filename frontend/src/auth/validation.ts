/** Registration rules from PROMPT_2 (mirrored by the server-side pydantic schema). */
export const LOGIN_RE = /^[A-Za-z0-9_]{3,20}$/;
export const MIN_PASSWORD_LENGTH = 6;

export function validateRegistration(login: string, password: string, repeat: string): string | null {
  if (!LOGIN_RE.test(login)) return "Логин: 3–20 символов, только a-z, 0-9 и _.";
  if (password.length < MIN_PASSWORD_LENGTH) return "Пароль — минимум 6 символов.";
  if (password !== repeat) return "Пароли не совпадают.";
  return null;
}
