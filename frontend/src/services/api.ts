/**
 * Typed client for the Engine 3 REST API.
 *
 * Set VITE_API_BASE_URL (and optionally VITE_API_PREFIX) in a .env file when
 * the backend is not on localhost:8000 with the default /api/v1 prefix.
 */

import { API_BASE, apiPath } from "./config";

export { API_BASE };

export type RenderMode = "svg" | "image";

export type NodeKind =
  | "bus"
  | "server"
  | "passenger"
  | "phone"
  | "database"
  | "cloud"
  | "service"
  | "sensor"
  | "generic";

export interface NodePosition {
  x: number;
  y: number;
}

export interface NodeSpec {
  id: string;
  label: string;
  kind: NodeKind;
  group?: string | null;
  position?: NodePosition | null;
  metadata?: Record<string, unknown>;
}

export interface EdgeSpec {
  id?: string | null;
  source: string;
  target: string;
  label?: string | null;
  style?: "solid" | "dashed";
  show_arrow?: boolean;
}

export interface DiagramOptions {
  direction: "LR" | "TB";
  show_edge_labels: boolean;
}

export interface VisualInstructionInput {
  schema_version?: string;
  scene_id: string;
  type?: "diagram";
  /** Output mode. Defaults to "svg"; image mode is always explicit. */
  render_mode?: RenderMode;
  title: string;
  description?: string | null;
  options?: Partial<DiagramOptions>;
  nodes: NodeSpec[];
  edges: EdgeSpec[];
}

export interface AssetSummary {
  asset_id: string;
  kind: "svg" | "image";
  content_hash: string;
  mime_type: string;
  width: number | null;
  height: number | null;
  category: string | null;
  tags: string[];
  provider: string;
  reuse_count: number;
  url: string;
  thumbnail_url: string;
  created_at: string;
  last_used_at: string;
}

export interface AssetListResponse {
  items: AssetSummary[];
  total: number;
}

export interface VisualizeResponse {
  scene_id: string;
  schema_version: string;
  render_mode: RenderMode;
  revision: number;
  cache_hit: boolean;
  asset: AssetSummary;
  /** Inline SVG for svg mode; always null for image mode. */
  svg: string | null;
  warnings: string[];
}

export interface SceneRevision {
  revision: number;
  asset_id: string;
  created_at: string;
}

export interface SceneDetail {
  scene_id: string;
  schema_version: string;
  render_mode: RenderMode;
  title: string;
  description: string | null;
  type: "diagram";
  options: DiagramOptions;
  nodes: NodeSpec[];
  edges: EdgeSpec[];
  current_revision: number;
  created_at: string;
  updated_at: string;
  revisions: SceneRevision[];
  current_asset: AssetSummary | null;
  svg: string | null;
  warnings: string[];
}

export interface HealthResponse {
  status: "ok" | "degraded";
  engine: string;
  version: string;
  database: string;
  storage: string;
  timestamp: string;
  schema_version: string;
  schema_versions: string[];
}

export interface NodePatch {
  id: string;
  label?: string;
  kind?: NodeKind;
  group?: string | null;
  position?: NodePosition | null;
  metadata?: Record<string, unknown>;
}

export interface SceneUpdatePayload {
  expected_revision?: number;
  /** Omit to keep the scene's stored mode; set to switch it explicitly. */
  render_mode?: RenderMode;
  title?: string;
  description?: string | null;
  options?: DiagramOptions;
  nodes_to_add?: NodeSpec[];
  nodes_to_update?: NodePatch[];
  nodes_to_remove?: string[];
  edges_to_add?: EdgeSpec[];
  edges_to_remove?: string[];
}

export class ApiError extends Error {
  readonly status?: number;
  readonly detail?: unknown;

  constructor(message: string, status?: number, detail?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers: Record<string, string> = { ...(init?.headers as Record<string, string> | undefined) };
  if (init?.body) {
    headers["Content-Type"] = "application/json";
  }
  if (!headers["X-Request-ID"] && typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    headers["X-Request-ID"] = crypto.randomUUID();
  }

  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, { ...init, headers });
  } catch {
    throw new ApiError(`Could not reach the Engine 3 backend at ${API_BASE}. Make sure it is running.`);
  }

  const text = await response.text();
  let data: unknown = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = text;
    }
  }

  if (!response.ok) {
    const detail =
      data !== null && typeof data === "object" && "detail" in data
        ? (data as { detail: unknown }).detail
        : data;
    throw new ApiError(`Request failed with HTTP ${response.status}`, response.status, detail);
  }

  return data as T;
}

export function getHealth(): Promise<HealthResponse> {
  return request<HealthResponse>(apiPath("/health"));
}

export function getSampleInstruction(): Promise<VisualInstructionInput> {
  return request<VisualInstructionInput>(apiPath("/samples/visual-instruction"));
}

export function postVisualize(instruction: unknown): Promise<VisualizeResponse> {
  return request<VisualizeResponse>(apiPath("/visualize"), {
    method: "POST",
    body: JSON.stringify(instruction),
  });
}

export function updateScene(sceneId: string, patch: SceneUpdatePayload): Promise<VisualizeResponse> {
  return request<VisualizeResponse>(apiPath(`/scenes/${encodeURIComponent(sceneId)}/update`), {
    method: "POST",
    body: JSON.stringify(patch),
  });
}

export function getScene(sceneId: string): Promise<SceneDetail> {
  return request<SceneDetail>(apiPath(`/scenes/${encodeURIComponent(sceneId)}`));
}

export function listAssets(): Promise<AssetListResponse> {
  return request<AssetListResponse>(apiPath("/assets?limit=40"));
}

export function assetUrl(path: string): string {
  return path.startsWith("http") ? path : `${API_BASE}${path}`;
}

/**
 * Human-readable hints for provider-facing statuses. The backend already
 * returns safe messages (no keys, prompts or raw provider bodies); these hints
 * only add actionable context and never replace the server detail.
 */
const PROVIDER_STATUS_HINTS: Partial<Record<number, string>> = {
  429: "The image provider is rate limiting requests. Wait a moment and try again.",
  503: "Image generation is unavailable. The backend image provider may not be configured.",
  504: "The image provider timed out. Try again.",
};

export function formatApiError(error: unknown): string {
  if (error instanceof ApiError) {
    let message: string;
    if (typeof error.detail === "string" && error.detail.trim()) {
      message = error.detail;
    } else if (Array.isArray(error.detail)) {
      const messages = error.detail.map((item) => {
        const entry = item as { loc?: unknown; msg?: unknown };
        const location = Array.isArray(entry.loc) ? entry.loc.slice(1).join(".") : "";
        const detailMessage = typeof entry.msg === "string" ? entry.msg : "Invalid value";
        return location ? `${location}: ${detailMessage}` : detailMessage;
      });
      message = messages.length > 0 ? messages.join("; ") : error.message;
    } else {
      message = error.message;
    }

    const hint = error.status !== undefined ? PROVIDER_STATUS_HINTS[error.status] : undefined;
    // A missing/storage 503 is not a provider-configuration problem; keep the
    // backend's precise recovery message without an image-provider hint.
    const isAssetRecovery503 =
      error.status === 503 && /missing|not regenerated|could not be recovered/i.test(message);
    if (hint && !isAssetRecovery503 && !message.includes(hint)) {
      return `${message} ${hint}`;
    }
    return message;
  }
  if (error instanceof Error) {
    return error.message;
  }
  return "Unexpected error.";
}
