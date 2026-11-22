import { useEffect, useRef, useState } from "react";

import { Toolbar } from "./components/Toolbar";
import { VisualCanvas } from "./components/VisualCanvas";
import {
  AlertIcon,
  ArrowRightIcon,
  BridgeMark,
  CheckIcon,
  CloseIcon,
  CodeIcon,
  DatabaseIcon,
  ImageIcon,
  SparkIcon,
} from "./components/icons";
import {
  API_BASE,
  assetUrl,
  formatApiError,
  getHealth,
  getSampleInstruction,
  getScene,
  listAssets,
  postVisualize,
  updateScene,
  type AssetSummary,
  type HealthResponse,
  type RenderMode,
  type SceneDetail,
  type SceneUpdatePayload,
  type VisualizeResponse,
} from "./services/api";

type BusyState = "idle" | "generate" | "update";

const MIN_ZOOM = 0.6;
const MAX_ZOOM = 2;

function prettyJson(value: unknown): string {
  return JSON.stringify(value, null, 2);
}

function shortAssetId(assetId: string): string {
  return assetId.length <= 18 ? assetId : `${assetId.slice(0, 12)}…${assetId.slice(-4)}`;
}

function instructionTextFromScene(scene: SceneDetail): string {
  return prettyJson({
    schema_version: scene.schema_version,
    scene_id: scene.scene_id,
    type: scene.type,
    render_mode: scene.render_mode ?? "svg",
    title: scene.title,
    description: scene.description,
    options: scene.options,
    nodes: scene.nodes,
    edges: scene.edges,
  });
}

const LAST_SCENE_KEY = "mindbridge.engine3.lastSceneId";

function readSavedSceneId(): string | null {
  try {
    return window.localStorage.getItem(LAST_SCENE_KEY);
  } catch {
    return null;
  }
}

function saveSceneId(sceneId: string): void {
  try {
    window.localStorage.setItem(LAST_SCENE_KEY, sceneId);
  } catch {
    // Persistence is best-effort (private browsing can reject storage).
  }
}

function forgetSceneId(): void {
  try {
    window.localStorage.removeItem(LAST_SCENE_KEY);
  } catch {
    // Ignore storage failures.
  }
}

function sceneIdOf(raw: unknown): string | null {
  if (typeof raw !== "object" || raw === null) {
    return null;
  }
  const value = (raw as { scene_id?: unknown }).scene_id;
  return typeof value === "string" ? value : null;
}

function resultFromScene(scene: SceneDetail): VisualizeResponse | null {
  if (!scene.current_asset) {
    return null;
  }
  return {
    scene_id: scene.scene_id,
    schema_version: scene.schema_version,
    render_mode: scene.render_mode ?? "svg",
    revision: scene.current_revision,
    cache_hit: true,
    asset: scene.current_asset,
    svg: scene.svg ?? null,
    warnings: scene.warnings ?? [],
  };
}

/**
 * The mode control is the source of truth for a fresh generation, so the
 * selected mode is sent explicitly even when the pasted JSON omits it.
 */
function withRenderMode(raw: unknown, mode: RenderMode): unknown {
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) {
    return raw;
  }
  return { ...(raw as Record<string, unknown>), render_mode: mode };
}

function fileExtensionForMime(mimeType: string): string {
  switch (mimeType.toLowerCase()) {
    case "image/jpeg":
    case "image/jpg":
      return "jpg";
    case "image/webp":
      return "webp";
    case "image/svg+xml":
      return "svg";
    default:
      return "png";
  }
}

function triggerDownload(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function buildUpdatePatch(scene: SceneDetail): SceneUpdatePayload {
  const expected_revision = scene.current_revision;
  const hasEta = scene.nodes.some((node) => node.id === "eta-engine");
  if (!hasEta) {
    return {
      expected_revision,
      nodes_to_add: [{ id: "eta-engine", label: "ETA Engine", kind: "service" }],
      edges_to_add: [
        { id: "gps-eta", source: "gps-server", target: "eta-engine", label: "Positions" },
      ],
    };
  }

  const passenger = scene.nodes.find((node) => node.id === "passenger");
  if (!passenger) {
    return { expected_revision, title: scene.title };
  }
  const isLive = passenger.label.includes("Live");
  return {
    expected_revision,
    nodes_to_update: [
      { id: "passenger", label: isLive ? "Passenger App" : "Passenger App · Live" },
    ],
  };
}

export default function App() {
  const [instructionText, setInstructionText] = useState("");
  const [result, setResult] = useState<VisualizeResponse | null>(null);
  const [scene, setScene] = useState<SceneDetail | null>(null);
  const [assets, setAssets] = useState<AssetSummary[]>([]);
  const [health, setHealth] = useState<HealthResponse | null | undefined>(undefined);
  const [busy, setBusy] = useState<BusyState>("idle");
  const [renderMode, setRenderMode] = useState<RenderMode>("svg");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [zoom, setZoom] = useState(1);
  const bootStarted = useRef(false);

  useEffect(() => {
    if (bootStarted.current) {
      return;
    }
    bootStarted.current = true;

    void (async () => {
      try {
        setHealth(await getHealth());
      } catch {
        setHealth(null);
      }

      try {
        const list = await listAssets();
        setAssets(list.items);
      } catch {
        // The asset panel is optional on boot; generation will retry it.
      }

      const savedSceneId = readSavedSceneId();
      if (savedSceneId) {
        try {
          const detail = await getScene(savedSceneId);
          const savedMode = detail.render_mode ?? "svg";
          setScene(detail);
          setInstructionText(instructionTextFromScene(detail));
          setRenderMode(savedMode);

          const restored = resultFromScene(detail);
          if (restored) {
            setResult(restored);
            setNotice(
              `Restored ${detail.scene_id} from revision ${detail.current_revision} without creating a new revision.`,
            );
            return;
          }

          if (savedMode === "image") {
            // A missing image asset must never trigger a paid regeneration.
            setResult(null);
            setNotice(
              "The saved image asset is unavailable. It is not regenerated automatically; select AI image mode again to request a new image.",
            );
            return;
          }

          // The SVG scene exists but its asset is unavailable: re-request it so
          // the backend can repair or replace the asset (unchanged scenes stay at
          // the same revision).
          await applyInstruction(JSON.parse(instructionTextFromScene(detail)), "svg");
          return;
        } catch {
          forgetSceneId();
        }
      }

      try {
        const sample = await getSampleInstruction();
        setInstructionText(prettyJson(sample));
        setRenderMode("svg");
        await applyInstruction(sample, "svg");
      } catch (err) {
        setError(formatApiError(err));
      }
    })();
    // Mount-only boot sequence; bootStarted guards against re-entry.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!notice) {
      return;
    }
    const timer = window.setTimeout(() => setNotice(null), 9000);
    return () => window.clearTimeout(timer);
  }, [notice]);

  async function applyInstruction(raw: unknown, mode: RenderMode = renderMode) {
    setBusy("generate");
    setError(null);
    setNotice(null);

    try {
      const previousRevision =
        scene && scene.scene_id === sceneIdOf(raw) ? scene.current_revision : null;
      const data = await postVisualize(withRenderMode(raw, mode));
      const revisionUnchanged = previousRevision === data.revision;
      setResult(data);
      setZoom(1);
      setRenderMode(data.render_mode ?? mode);
      saveSceneId(data.scene_id);
      setNotice(
        revisionUnchanged && data.scene_id === sceneIdOf(raw)
          ? `No changes detected — scene is still at revision ${data.revision}.`
          : data.cache_hit
            ? `Exact match found — reused ${shortAssetId(data.asset.asset_id)} from Visual Asset Memory (no regeneration).`
            : data.render_mode === "image"
              ? `Generated a ${data.asset.width ?? "–"}×${data.asset.height ?? "–"} image and stored it in Visual Asset Memory.`
              : `Rendered a ${data.asset.width ?? "–"}×${data.asset.height ?? "–"} SVG and stored it in Visual Asset Memory.`,
      );

      const [detail, list] = await Promise.all([getScene(data.scene_id), listAssets()]);
      setScene(detail);
      setAssets(list.items);
      setInstructionText(instructionTextFromScene(detail));
    } catch (err) {
      setError(formatApiError(err));
    } finally {
      setBusy("idle");
    }
  }

  function handleGenerate() {
    if (busy !== "idle") {
      return;
    }
    if (!instructionText.trim()) {
      setError("Paste a visual instruction payload before generating.");
      return;
    }

    let parsed: unknown;
    try {
      parsed = JSON.parse(instructionText);
    } catch (err) {
      setError(
        `Visual instruction is not valid JSON: ${err instanceof Error ? err.message : "parse error"}`,
      );
      return;
    }

    void applyInstruction(parsed, renderMode);
  }

  async function handleLoadSample() {
    if (busy !== "idle") {
      return;
    }
    setBusy("generate");
    setError(null);
    setNotice(null);
    setRenderMode("svg");

    try {
      const sample = await getSampleInstruction();
      setInstructionText(prettyJson(sample));
      await applyInstruction(sample, "svg");
    } catch (err) {
      setError(formatApiError(err));
    } finally {
      setBusy("idle");
    }
  }

  async function handleSimulateUpdate() {
    if (!scene || busy !== "idle") {
      return;
    }
    setBusy("update");
    setError(null);
    setNotice(null);

    try {
      const previousRevision = scene.current_revision;
      const data = await updateScene(scene.scene_id, buildUpdatePatch(scene));
      setResult(data);
      setZoom(1);
      setRenderMode(data.render_mode ?? renderMode);
      saveSceneId(data.scene_id);
      setNotice(
        data.revision === previousRevision
          ? `No changes detected — scene is still at revision ${data.revision}.`
          : `Scene updated — revision ${data.revision} is now live on the canvas.`,
      );

      const [detail, list] = await Promise.all([getScene(scene.scene_id), listAssets()]);
      setScene(detail);
      setAssets(list.items);
      setInstructionText(instructionTextFromScene(detail));
    } catch (err) {
      setError(formatApiError(err));
    } finally {
      setBusy("idle");
    }
  }

  async function handleRenderModeChange(next: RenderMode) {
    if (next === renderMode || busy !== "idle") {
      return;
    }

    const previous = renderMode;
    setRenderMode(next);
    setError(null);
    setNotice(null);

    if (!scene) {
      // No saved scene yet: the next Generate request uses the selected mode.
      return;
    }

    setBusy("update");
    let updated = false;
    try {
      // Switching modes is a scene revision and must obey expected_revision.
      const data = await updateScene(scene.scene_id, {
        expected_revision: scene.current_revision,
        render_mode: next,
      });
      updated = true;
      setResult(data);
      setZoom(1);
      setRenderMode(data.render_mode ?? next);
      saveSceneId(data.scene_id);
      setNotice(
        data.render_mode === "image"
          ? `Switched to AI image mode — revision ${data.revision} is now live on the canvas.`
          : `Switched to SVG mode — revision ${data.revision} is now live on the canvas.`,
      );

      const [detail, list] = await Promise.all([getScene(scene.scene_id), listAssets()]);
      setScene(detail);
      setAssets(list.items);
      setInstructionText(instructionTextFromScene(detail));
    } catch (err) {
      // Revert only when the switch itself failed (for example a provider 503);
      // a failed follow-up refresh must not undo a successful mode change.
      if (!updated) {
        setRenderMode(previous);
      }
      setError(formatApiError(err));
    } finally {
      setBusy("idle");
    }
  }

  async function handleDownload() {
    if (!result) {
      return;
    }

    if (result.svg) {
      triggerDownload(
        new Blob([result.svg], { type: "image/svg+xml;charset=utf-8" }),
        `${result.scene_id}-revision-${result.revision}.svg`,
      );
      return;
    }

    if (result.asset.kind !== "image") {
      return;
    }

    const href = assetUrl(result.asset.url);
    const filename = `${result.scene_id}-revision-${result.revision}.${fileExtensionForMime(
      result.asset.mime_type,
    )}`;
    try {
      const response = await fetch(href);
      if (!response.ok) {
        throw new Error(`Asset download failed with HTTP ${response.status}`);
      }
      triggerDownload(await response.blob(), filename);
    } catch {
      // If the asset response cannot be read as a blob (for example a CORS
      // restriction), fall back to opening the immutable asset URL directly.
      window.open(href, "_blank", "noopener,noreferrer");
    }
  }

  const isBusy = busy !== "idle";
  const healthLabel =
    health === undefined
      ? "Connecting"
      : health === null
        ? "Backend offline"
        : health.status === "ok"
          ? "Backend online"
          : "Backend degraded";
  const healthClass = health === null ? "offline" : health?.status === "degraded" ? "warn" : "";
  const imageAssets = assets.filter((asset) => asset.kind === "image");
  const currentAssetId = result?.asset.asset_id;
  const currentImage = result?.asset.kind === "image" ? result.asset : null;
  const currentImageUrl = currentImage ? assetUrl(currentImage.url) : null;
  const otherImageAssets = imageAssets.filter((asset) => asset.asset_id !== currentImage?.asset_id);
  const imageUnavailable = Boolean(
    currentImage && result?.warnings?.some((warning) => /not regenerated|missing/i.test(warning)),
  );
  const canDownload = Boolean(result && (result.svg || result.asset.kind === "image"));

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark">
            <BridgeMark size={21} />
          </span>
          <div className="brand-text">
            <div className="brand-line">
              <span className="brand-name">Mind Bridge</span>
              <span className="engine-chip">ENGINE 3</span>
            </div>
            <span className="brand-sub">Visualization Engine</span>
          </div>
        </div>
        <div className="topbar-meta">
          <a className="topbar-link" href={`${API_BASE}/docs`} target="_blank" rel="noreferrer">
            API docs
          </a>
          <span className={`status-pill ${healthClass}`}>
            <span className="status-dot" />
            {healthLabel}
            {health ? (
              <>
                <span className="status-sep">/</span>
                <span className="status-version">v{health.version}</span>
              </>
            ) : null}
          </span>
        </div>
      </header>

      <main className="workspace">
        <aside className="panel instruction-panel" aria-label="Visual instruction">
          <div className="panel-head">
            <div>
              <h2>Visual instruction</h2>
              <p>Structured JSON from Engine 2</p>
            </div>
            <span className="tag mono">
              <CodeIcon size={12} />
              JSON
            </span>
          </div>

          <label className="editor-label" htmlFor="instruction-json">
            Instruction payload
          </label>
          <textarea
            id="instruction-json"
            className="json-editor"
            value={instructionText}
            onChange={(event) => setInstructionText(event.target.value)}
            spellCheck={false}
            autoCapitalize="off"
            autoCorrect="off"
            placeholder='{ "scene_id": "…", "title": "…", "nodes": [], "edges": [] }'
          />

          <div className="mode-control">
            <span className="mode-label" id="render-mode-label">
              Render mode
            </span>
            <div className="mode-options" role="group" aria-labelledby="render-mode-label">
              <button
                type="button"
                className={`mode-option${renderMode === "svg" ? " active" : ""}`}
                aria-pressed={renderMode === "svg"}
                onClick={() => void handleRenderModeChange("svg")}
                disabled={isBusy}
              >
                SVG diagram
              </button>
              <button
                type="button"
                className={`mode-option${renderMode === "image" ? " active" : ""}`}
                aria-pressed={renderMode === "image"}
                onClick={() => void handleRenderModeChange("image")}
                disabled={isBusy}
              >
                AI image
              </button>
            </div>
            <p className="mode-hint">
              {scene
                ? "Switching mode creates a new revision using the scene's expected_revision."
                : "SVG is the default. AI image mode is explicit and uses the configured provider."}
            </p>
          </div>

          <div className="action-row">
            <button
              type="button"
              className="btn ghost"
              onClick={() => void handleLoadSample()}
              disabled={isBusy}
            >
              Load sample
            </button>
            <button type="button" className="btn primary" onClick={handleGenerate} disabled={isBusy}>
              {busy === "generate" ? <span className="btn-spinner" /> : <SparkIcon size={15} />}
              {busy === "generate" ? "Rendering…" : "Generate visual"}
            </button>
          </div>

          <div className="pipeline" aria-label="Rendering pipeline">
            <span className="pipeline-step">
              <span className="pipeline-dot" />
              Validate
            </span>
            <span className="pipeline-arrow">›</span>
            <span className="pipeline-step">
              <span className="pipeline-dot" />
              Plan
            </span>
            <span className="pipeline-arrow">›</span>
            <span className="pipeline-step">
              <span className="pipeline-dot" />
              Render
            </span>
            <span className="pipeline-arrow">›</span>
            <span className="pipeline-step">
              <span className="pipeline-dot" />
              Remember
            </span>
          </div>

          <div className="section-divider" />

          <div className="panel-head compact">
            <div>
              <h3>Scene state</h3>
              <p>Server-managed revision history</p>
            </div>
            <span className="tag">{scene ? `rev ${scene.current_revision}` : "no scene"}</span>
          </div>

          <dl className="meta-list">
            <div className="meta-row">
              <dt>Scene</dt>
              <dd className="mono">{result?.scene_id ?? "—"}</dd>
            </div>
            <div className="meta-row">
              <dt>Schema</dt>
              <dd className="mono">{result?.schema_version ?? "—"}</dd>
            </div>
            <div className="meta-row">
              <dt>Mode</dt>
              <dd>{result ? (result.render_mode === "image" ? "AI image" : "SVG") : "—"}</dd>
            </div>
            <div className="meta-row">
              <dt>Revision</dt>
              <dd>{result ? result.revision : "—"}</dd>
            </div>
            <div className="meta-row">
              <dt>Asset</dt>
              <dd className="mono">{result ? shortAssetId(result.asset.asset_id) : "—"}</dd>
            </div>
            <div className="meta-row">
              <dt>Source</dt>
              <dd>{result ? (result.cache_hit ? "Asset memory" : "Fresh render") : "—"}</dd>
            </div>
            <div className="meta-row">
              <dt>Size</dt>
              <dd>
                {result?.asset.width && result.asset.height
                  ? `${result.asset.width} × ${result.asset.height} px`
                  : "—"}
              </dd>
            </div>
          </dl>

          <button
            type="button"
            className="btn secondary full"
            onClick={() => void handleSimulateUpdate()}
            disabled={!scene || isBusy}
          >
            {busy === "update" ? "Applying update…" : "Simulate scene update"}
            {busy !== "update" && <ArrowRightIcon size={14} />}
          </button>
          <p className="hint">
            Adds an ETA service on the first run, then toggles the passenger label. Every change
            becomes a new scene revision.
          </p>

          <div className="integration-note">
            <span className="integration-head">Engine 2 integration</span>
            <p>
              Send this payload to <code>POST /api/v1/visualize</code>. The full contract is in{" "}
              <code>docs/ARCHITECTURE.md</code>.
            </p>
            <a href={`${API_BASE}/docs`} target="_blank" rel="noreferrer">
              Open interactive API docs ↗
            </a>
          </div>
        </aside>

        <section className="panel canvas-panel" aria-label="Visual canvas">
          <Toolbar
            sceneId={result?.scene_id}
            revision={result?.revision}
            cacheHit={result?.cache_hit}
            renderMode={result?.render_mode ?? renderMode}
            zoom={zoom}
            canDownload={canDownload}
            onZoomIn={() => setZoom((value) => Math.min(MAX_ZOOM, Math.round((value + 0.1) * 10) / 10))}
            onZoomOut={() => setZoom((value) => Math.max(MIN_ZOOM, Math.round((value - 0.1) * 10) / 10))}
            onZoomReset={() => setZoom(1)}
            onDownload={() => void handleDownload()}
          />

          {error ? (
            <div className="banner error" role="alert">
              <AlertIcon size={15} className="banner-icon" />
              <span>{error}</span>
              <button
                type="button"
                className="banner-close"
                onClick={() => setError(null)}
                aria-label="Dismiss error"
              >
                <CloseIcon size={14} />
              </button>
            </div>
          ) : null}

          {notice ? (
            <div className="banner success" role="status">
              <CheckIcon size={15} className="banner-icon" />
              <span>{notice}</span>
              <button
                type="button"
                className="banner-close"
                onClick={() => setNotice(null)}
                aria-label="Dismiss message"
              >
                <CloseIcon size={14} />
              </button>
            </div>
          ) : null}

          {result?.warnings && result.warnings.length > 0 ? (
            <div className="banner warning" role="status" aria-live="polite">
              <AlertIcon size={15} className="banner-icon" />
              <span>{result.warnings.join(" ")}</span>
            </div>
          ) : null}

          <VisualCanvas
            svg={result?.svg ?? null}
            imageUrl={currentImageUrl}
            imageAlt={result ? `AI-generated visual for ${result.scene_id}` : undefined}
            imageUnavailable={imageUnavailable}
            loading={isBusy}
            loadingLabel={
              renderMode === "image"
                ? "Requesting the configured image provider · storing the asset"
                : "Validating · planning · rendering"
            }
            zoom={zoom}
            onGenerateSample={() => void handleLoadSample()}
          />
        </section>

        <aside className="panel assets-panel" aria-label="Visual asset memory">
          <div className="panel-head">
            <div>
              <h2>Asset memory</h2>
              <p>Exact-match reuse · newest first</p>
            </div>
            <span className="tag">{assets.length}</span>
          </div>

          <div className="asset-list">
            {assets.length === 0 ? (
              <div className="asset-empty">
                <DatabaseIcon size={20} />
                <p>No stored assets yet</p>
                <span>Generate a visual to populate memory.</span>
              </div>
            ) : (
              assets.map((asset) => (
                <a
                  key={asset.asset_id}
                  className={`asset-item${asset.asset_id === currentAssetId ? " current" : ""}`}
                  href={assetUrl(asset.url)}
                  target="_blank"
                  rel="noreferrer"
                  title="Open asset in a new tab"
                >
                  <span className="asset-thumb">
                    <img src={assetUrl(asset.thumbnail_url)} alt="" loading="lazy" />
                  </span>
                  <span className="asset-body">
                    <span className="asset-top">
                      <span className="asset-kind">{asset.kind.toUpperCase()}</span>
                      {asset.asset_id === currentAssetId ? (
                        <span className="tag current">current</span>
                      ) : null}
                    </span>
                    <span className="asset-id">{shortAssetId(asset.asset_id)}</span>
                    <span className="asset-meta">
                      {asset.width ?? "?"}×{asset.height ?? "?"} · reused {asset.reuse_count}×
                    </span>
                  </span>
                </a>
              ))
            )}
          </div>

          <div className="image-block">
            <div className="panel-head compact">
              <div>
                <h3>Image preview</h3>
                <p>AI-generated visuals</p>
              </div>
              <span className={`tag ${currentImage ? "current" : "muted"}`}>
                {currentImage
                  ? "current"
                  : imageAssets.length > 0
                    ? `${imageAssets.length} stored`
                    : "empty"}
              </span>
            </div>

            {currentImage ? (
              <a
                className="image-current"
                href={assetUrl(currentImage.url)}
                target="_blank"
                rel="noreferrer"
                title="Open the current image in a new tab"
              >
                <img src={assetUrl(currentImage.url)} alt="Current AI-generated scene" />
                <span className="image-current-meta">
                  {currentImage.mime_type} · {currentImage.width ?? "?"}×{currentImage.height ?? "?"}
                </span>
              </a>
            ) : null}

            {otherImageAssets.length > 0 ? (
              <div className="image-grid">
                {otherImageAssets.map((asset) => (
                  <a
                    key={asset.asset_id}
                    href={assetUrl(asset.url)}
                    target="_blank"
                    rel="noreferrer"
                  >
                    <img
                      src={assetUrl(asset.thumbnail_url)}
                      alt="Generated image asset"
                      loading="lazy"
                    />
                  </a>
                ))}
              </div>
            ) : null}

            {!currentImage && imageAssets.length === 0 ? (
              <div className="image-empty">
                <ImageIcon size={22} />
                <p>No AI images yet</p>
                <span>
                  Select “AI image” to generate one. This requires the backend image provider
                  (OPENAI_API_KEY or GEMINI_API_KEY); without it, image requests return a safe
                  error and SVG rendering still works.
                </span>
              </div>
            ) : null}
          </div>
        </aside>
      </main>
    </div>
  );
}
