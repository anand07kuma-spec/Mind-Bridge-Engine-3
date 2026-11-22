/**
 * Frontend runtime configuration, resolved from Vite environment variables.
 *
 * Canonical variable: ``VITE_API_BASE_URL`` (matches the README contract).
 * Deprecated alias: ``VITE_API_BASE`` (older builds only; still honoured so an
 * existing .env keeps working).
 *
 * ``VITE_API_PREFIX`` must match the backend's ``MIND_BRIDGE_API_PREFIX``
 * (default ``/api/v1``). Asset URLs returned by the API are always prefixed
 * server-side, so they are resolved against the API origin only.
 */

export const DEFAULT_API_BASE = "http://localhost:8000";
export const DEFAULT_API_PREFIX = "/api/v1";

export function resolveApiBase(
  canonical: string | undefined,
  legacy: string | undefined,
  fallback: string = DEFAULT_API_BASE,
): string {
  const selected = (canonical ?? "").trim() || (legacy ?? "").trim() || fallback;
  return selected.replace(/\/+$/, "");
}

export function normalizeApiPrefix(raw: string | undefined): string {
  const value = (raw ?? "").trim().replace(/^\/+|\/+$/g, "");
  return value ? `/${value}` : "";
}

export const API_BASE = resolveApiBase(
  import.meta.env.VITE_API_BASE_URL,
  import.meta.env.VITE_API_BASE,
);

export const API_PREFIX = normalizeApiPrefix(import.meta.env.VITE_API_PREFIX ?? DEFAULT_API_PREFIX);

export function apiPath(path: string): string {
  return `${API_PREFIX}${path.startsWith("/") ? path : `/${path}`}`;
}
