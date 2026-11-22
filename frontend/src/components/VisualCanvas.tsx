import { useState } from "react";

import { BridgeMark, ImageIcon, SparkIcon } from "./icons";

interface VisualCanvasProps {
  svg: string | null;
  imageUrl?: string | null;
  imageAlt?: string;
  imageUnavailable?: boolean;
  loading: boolean;
  loadingLabel?: string;
  zoom: number;
  onGenerateSample: () => void;
}

export function VisualCanvas({
  svg,
  imageUrl = null,
  imageAlt = "AI-generated visual",
  imageUnavailable = false,
  loading,
  loadingLabel = "Validating · planning · rendering",
  zoom,
  onGenerateSample,
}: VisualCanvasProps) {
  const zoomed = zoom > 1.001;
  const [failedUrl, setFailedUrl] = useState<string | null>(null);

  const showImage = Boolean(imageUrl) && failedUrl !== imageUrl && !imageUnavailable;

  return (
    <div className="canvas-stage" aria-busy={loading}>
      <div className={`canvas-scroll${zoomed ? " zoomed" : ""}`}>
        {svg ? (
          <div className="diagram-shell" style={{ width: `${Math.round(zoom * 100)}%` }}>
            {/* Engine 3 generates this SVG server-side and escapes all user text. */}
            <div className="diagram-host" dangerouslySetInnerHTML={{ __html: svg }} />
          </div>
        ) : showImage ? (
          <div
            className="diagram-shell image-shell"
            style={{ width: `${Math.round(zoom * 100)}%` }}
          >
            {/* Raster images are always rendered as <img>, never as inline HTML. */}
            <img
              className="image-host"
              src={imageUrl ?? undefined}
              alt={imageAlt}
              onError={() => setFailedUrl(imageUrl)}
            />
          </div>
        ) : imageUnavailable || failedUrl === imageUrl ? (
          <div className="canvas-empty" role="status">
            <span className="canvas-empty-icon">
              <ImageIcon size={26} />
            </span>
            <h3>Image unavailable</h3>
            <p>
              The generated image file is missing. Engine 3 does not regenerate images
              automatically; select the AI image mode again to request a new image explicitly.
            </p>
          </div>
        ) : (
          !loading && (
            <div className="canvas-empty">
              <span className="canvas-empty-icon">
                <BridgeMark size={26} />
              </span>
              <h3>No visual on the canvas</h3>
              <p>
                Generate the bundled bus-tracking scene to render a deterministic SVG or an
                AI-generated image.
              </p>
              <button type="button" className="btn primary" onClick={onGenerateSample}>
                <SparkIcon size={14} />
                Generate sample visual
              </button>
            </div>
          )
        )}
      </div>
      {loading && (
        <div className="canvas-loading" role="status">
          <span className="spinner" />
          <span>{loadingLabel}</span>
        </div>
      )}
    </div>
  );
}
