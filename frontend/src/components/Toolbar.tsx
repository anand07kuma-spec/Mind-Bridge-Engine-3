import type { RenderMode } from "../services/api";
import { DownloadIcon, ResetIcon, ZoomInIcon, ZoomOutIcon } from "./icons";

interface ToolbarProps {
  sceneId?: string;
  revision?: number;
  cacheHit?: boolean;
  renderMode?: RenderMode;
  zoom: number;
  canDownload: boolean;
  onZoomIn: () => void;
  onZoomOut: () => void;
  onZoomReset: () => void;
  onDownload: () => void;
}

export function Toolbar({
  sceneId,
  revision,
  cacheHit,
  renderMode,
  zoom,
  canDownload,
  onZoomIn,
  onZoomOut,
  onZoomReset,
  onDownload,
}: ToolbarProps) {
  const isImage = renderMode === "image";

  return (
    <div className="toolbar">
      <span className="toolbar-title">Canvas</span>
      <div className="toolbar-chips">
        {sceneId && (
          <span className="chip" title={sceneId}>
            {sceneId}
          </span>
        )}
        {typeof revision === "number" && <span className="chip accent">rev {revision}</span>}
        {renderMode && (
          <span className={`chip ${isImage ? "accent" : ""}`}>
            {isImage ? "AI image" : "SVG"}
          </span>
        )}
        {cacheHit && (
          <span className="chip success" title="Identical visual reused from Visual Asset Memory">
            reused asset
          </span>
        )}
      </div>
      <span className="toolbar-spacer" />
      <div className="zoom-group" role="group" aria-label="Canvas zoom">
        <button
          type="button"
          className="icon-btn"
          onClick={onZoomOut}
          aria-label="Zoom out"
          disabled={zoom <= 0.6}
        >
          <ZoomOutIcon size={15} />
        </button>
        <span className="zoom-value" aria-live="polite">
          {Math.round(zoom * 100)}%
        </span>
        <button
          type="button"
          className="icon-btn"
          onClick={onZoomIn}
          aria-label="Zoom in"
          disabled={zoom >= 2}
        >
          <ZoomInIcon size={15} />
        </button>
      </div>
      <button
        type="button"
        className="icon-btn"
        onClick={onZoomReset}
        aria-label="Fit diagram to canvas"
        title="Fit to canvas"
        disabled={!sceneId}
      >
        <ResetIcon size={15} />
      </button>
      <span className="toolbar-divider" />
      <button
        type="button"
        className="btn secondary btn-compact"
        onClick={onDownload}
        disabled={!canDownload}
      >
        <DownloadIcon size={14} />
        {isImage ? "Download image" : "Download SVG"}
      </button>
    </div>
  );
}
