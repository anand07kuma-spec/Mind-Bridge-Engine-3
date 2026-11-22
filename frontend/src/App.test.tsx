import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "./App";

const sample = {
  schema_version: "1.0",
  scene_id: "bus-tracking-demo",
  type: "diagram",
  title: "Live Bus Tracking System",
  description: "Three buses stream GPS coordinates to the central server.",
  options: { direction: "LR", show_edge_labels: true },
  nodes: [
    { id: "bus-1", label: "Bus 101", kind: "bus" },
    { id: "gps-server", label: "GPS Server", kind: "server" },
    { id: "passenger", label: "Passenger App", kind: "passenger" },
  ],
  edges: [
    { id: "bus1-gps", source: "bus-1", target: "gps-server", label: "GPS ping" },
    { id: "gps-passenger", source: "gps-server", target: "passenger", label: "Live location" },
  ],
};

const asset = {
  asset_id: "ast_1234567890abcdef",
  kind: "svg",
  content_hash: "hash",
  mime_type: "image/svg+xml",
  width: 920,
  height: 516,
  category: "diagram",
  tags: ["diagram", "svg"],
  provider: "engine3-svg",
  reuse_count: 0,
  url: "/api/v1/assets/ast_1234567890abcdef/content",
  thumbnail_url: "/api/v1/assets/ast_1234567890abcdef/thumbnail",
  created_at: "2026-11-19T12:00:00Z",
  last_used_at: "2026-11-19T12:00:00Z",
};

const imageAsset = {
  ...asset,
  asset_id: "ast_image0123456789",
  kind: "image",
  content_hash: "image-hash",
  mime_type: "image/png",
  width: 1024,
  height: 1024,
  tags: ["diagram", "image"],
  provider: "openai",
  url: "/api/v1/assets/ast_image0123456789/content",
  thumbnail_url: "/api/v1/assets/ast_image0123456789/thumbnail",
};

const svg =
  '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 920 516"><title>Live Bus Tracking System</title><text>Bus 101</text></svg>';

const visualizeResponse = {
  scene_id: "bus-tracking-demo",
  schema_version: "1.0",
  render_mode: "svg" as const,
  revision: 1,
  cache_hit: false,
  asset,
  svg,
  warnings: [] as string[],
};

const imageVisualizeResponse = {
  scene_id: "bus-tracking-demo",
  schema_version: "1.0",
  render_mode: "image" as const,
  revision: 2,
  cache_hit: false,
  asset: imageAsset,
  svg: null,
  warnings: [] as string[],
};

const sceneDetail = {
  scene_id: "bus-tracking-demo",
  schema_version: "1.0",
  render_mode: "svg" as const,
  title: "Live Bus Tracking System",
  description: "Three buses stream GPS coordinates to the central server.",
  type: "diagram",
  options: { direction: "LR", show_edge_labels: true },
  nodes: sample.nodes,
  edges: sample.edges,
  current_revision: 1,
  created_at: "2026-11-19T12:00:00Z",
  updated_at: "2026-11-19T12:00:00Z",
  revisions: [{ revision: 1, asset_id: asset.asset_id, created_at: "2026-11-19T12:00:00Z" }],
  current_asset: asset,
  svg,
  warnings: [] as string[],
};

const imageSceneDetail = {
  ...sceneDetail,
  render_mode: "image" as const,
  current_revision: 2,
  revisions: [
    ...sceneDetail.revisions,
    { revision: 2, asset_id: imageAsset.asset_id, created_at: "2026-11-19T12:01:00Z" },
  ],
  current_asset: imageAsset,
  svg: null,
  warnings: [] as string[],
};

const health = {
  status: "ok",
  engine: "mindbridge-engine3",
  version: "0.1.0",
  database: "ok",
  storage: "ok",
  timestamp: "2026-11-19T12:00:00Z",
  schema_version: "1.0",
  schema_versions: ["1.0"],
};

function jsonResponse(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function binaryResponse(bytes = [0x89, 0x50, 0x4e, 0x47], type = "image/png"): Response {
  return new Response(new Uint8Array(bytes), {
    status: 200,
    headers: { "Content-Type": type },
  });
}

const warning = "Some nodes had manual positions; falling back to automatic layout.";
const LAST_SCENE_KEY = "mindbridge.engine3.lastSceneId";

let fetchMock: ReturnType<typeof vi.fn>;
let visualizePayload: typeof visualizeResponse | typeof imageVisualizeResponse;
let scenePayload: typeof sceneDetail | typeof imageSceneDetail;
let updatePayload: typeof visualizeResponse | typeof imageVisualizeResponse;
let assetsPayload: { items: Array<typeof asset | typeof imageAsset>; total: number };
let visualizeResponder: (() => Response) | null;
let updateResponder: (() => Response) | null;
let assetContentResponder: (() => Response) | null;

type FetchCall = [RequestInfo | URL, RequestInit | undefined];

function callsTo(fragment: string): FetchCall[] {
  return fetchMock.mock.calls.filter(([input]) => String(input).includes(fragment)) as FetchCall[];
}

function requestBody(call: FetchCall): Record<string, unknown> {
  return JSON.parse(String(call[1]?.body));
}

beforeEach(() => {
  window.localStorage.clear();
  visualizePayload = visualizeResponse;
  scenePayload = sceneDetail;
  updatePayload = { ...visualizeResponse, cache_hit: true };
  assetsPayload = { items: [asset], total: 1 };
  visualizeResponder = null;
  updateResponder = null;
  assetContentResponder = null;

  Object.defineProperty(URL, "createObjectURL", {
    configurable: true,
    value: vi.fn(() => "blob:mock-download"),
  });
  Object.defineProperty(URL, "revokeObjectURL", {
    configurable: true,
    value: vi.fn(),
  });

  fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.endsWith("/api/v1/health")) return jsonResponse(health);
    if (url.includes("/samples/visual-instruction")) return jsonResponse(sample);
    if (url.includes("/update")) {
      return updateResponder ? updateResponder() : jsonResponse(updatePayload);
    }
    if (url.includes("/visualize")) {
      return visualizeResponder ? visualizeResponder() : jsonResponse(visualizePayload);
    }
    if (url.includes("/scenes/")) return jsonResponse(scenePayload);
    if (url.includes("/content")) {
      return assetContentResponder ? assetContentResponder() : binaryResponse();
    }
    if (url.includes("/api/v1/assets")) return jsonResponse(assetsPayload);
    throw new Error(`Unexpected request: ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  window.localStorage.clear();
});

describe("Engine 3 workspace", () => {
  it("boots from the sample instruction and displays the rendered SVG", async () => {
    render(<App />);

    expect(await screen.findByText("Backend online")).toBeTruthy();
    await waitFor(() => {
      expect(document.querySelector(".diagram-host svg")).toBeTruthy();
    });

    expect(document.querySelector(".diagram-host")?.innerHTML).toContain("Bus 101");
    expect(screen.getAllByText(/rev 1/).length).toBeGreaterThan(0);
    expect(screen.getByText("1.0")).toBeTruthy();
    expect(screen.getByRole("button", { name: /generate visual/i })).toBeTruthy();
    expect(screen.getByText(/reused 0×/)).toBeTruthy();
    expect(screen.getByRole("button", { name: /download svg/i })).not.toHaveProperty(
      "disabled",
      true,
    );
    expect(window.localStorage.getItem(LAST_SCENE_KEY)).toBe("bus-tracking-demo");
  });

  it("keeps SVG selected by default and sends render_mode explicitly", async () => {
    render(<App />);
    await waitFor(() => expect(document.querySelector(".diagram-host svg")).toBeTruthy());

    const svgButton = screen.getByRole("button", { name: /svg diagram/i });
    expect(svgButton.getAttribute("aria-pressed")).toBe("true");

    const visualizeCall = callsTo("/visualize")[0];
    expect(visualizeCall).toBeTruthy();
    expect(requestBody(visualizeCall).render_mode).toBe("svg");
    expect(document.querySelector(".diagram-host svg")).toBeTruthy();
    expect(document.querySelector(".image-host")).toBeNull();
  });

  it("restores the saved scene without creating a new revision on boot", async () => {
    window.localStorage.setItem(LAST_SCENE_KEY, "bus-tracking-demo");
    render(<App />);

    expect(
      await screen.findByText(/Restored bus-tracking-demo from revision 1/),
    ).toBeTruthy();
    await waitFor(() => {
      expect(document.querySelector(".diagram-host svg")).toBeTruthy();
    });

    expect(callsTo("/visualize")).toHaveLength(0);
    expect(callsTo("/update")).toHaveLength(0);
    expect(screen.getByRole("button", { name: /svg diagram/i }).getAttribute("aria-pressed")).toBe(
      "true",
    );
  });

  it("reports invalid JSON without calling the API again", async () => {
    render(<App />);

    const editor = (await screen.findByLabelText("Instruction payload")) as HTMLTextAreaElement;
    await waitFor(() => expect(editor.value).toContain("bus-tracking-demo"));

    const callsBefore = fetchMock.mock.calls.length;
    fireEvent.change(editor, { target: { value: "{ not json" } });
    fireEvent.click(screen.getByRole("button", { name: /generate visual/i }));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("not valid JSON");
    expect(fetchMock.mock.calls.length).toBe(callsBefore);
  });

  it("shows warnings alongside the success notice", async () => {
    visualizePayload = { ...visualizeResponse, warnings: [warning] };
    scenePayload = { ...sceneDetail, warnings: [warning] };
    render(<App />);

    expect(await screen.findByText(warning)).toBeTruthy();
    expect(screen.getByText(/Rendered a 920×516 SVG/)).toBeTruthy();
  });

  it("reports a no-op update without claiming a new revision", async () => {
    render(<App />);
    await screen.findByText(/Rendered a 920×516 SVG/);

    fireEvent.click(screen.getByRole("button", { name: /simulate scene update/i }));

    expect(await screen.findByText(/No changes detected/)).toBeTruthy();

    const updateCall = callsTo("/update")[0];
    expect(updateCall).toBeTruthy();
    expect(requestBody(updateCall).expected_revision).toBe(1);
  });

  it("switches modes with the saved expected_revision and shows the image asset URL", async () => {
    window.localStorage.setItem(LAST_SCENE_KEY, "bus-tracking-demo");
    render(<App />);
    await screen.findByText(/Restored bus-tracking-demo from revision 1/);

    updatePayload = imageVisualizeResponse;
    scenePayload = imageSceneDetail;
    assetsPayload = { items: [imageAsset], total: 1 };

    fireEvent.click(screen.getByRole("button", { name: /ai image/i }));

    expect(await screen.findByText(/Switched to AI image mode/)).toBeTruthy();

    const updateCall = callsTo("/update")[0];
    expect(updateCall).toBeTruthy();
    const body = requestBody(updateCall);
    expect(body.expected_revision).toBe(1);
    expect(body.render_mode).toBe("image");

    await waitFor(() => {
      const image = document.querySelector(".image-host");
      expect(image).toBeTruthy();
      expect(image?.getAttribute("src")).toContain(imageAsset.url);
    });
    expect(document.querySelector(".diagram-host svg")).toBeNull();
    expect(screen.getByRole("button", { name: /ai image/i }).getAttribute("aria-pressed")).toBe(
      "true",
    );
    expect(screen.getByRole("button", { name: /download image/i })).not.toHaveProperty(
      "disabled",
      true,
    );
  });

  it("sends render_mode=image on a fresh image generation request", async () => {
    window.localStorage.setItem(LAST_SCENE_KEY, "bus-tracking-demo");
    render(<App />);
    await screen.findByText(/Restored bus-tracking-demo from revision 1/);

    updatePayload = imageVisualizeResponse;
    scenePayload = imageSceneDetail;
    assetsPayload = { items: [imageAsset], total: 1 };
    fireEvent.click(screen.getByRole("button", { name: /ai image/i }));
    await screen.findByText(/Switched to AI image mode/);

    const editor = screen.getByLabelText("Instruction payload") as HTMLTextAreaElement;
    const payload = JSON.parse(editor.value);
    payload.scene_id = "fresh-image-scene";
    fireEvent.change(editor, { target: { value: JSON.stringify(payload) } });

    visualizeResponder = () =>
      jsonResponse({ ...imageVisualizeResponse, scene_id: "fresh-image-scene" });
    scenePayload = { ...imageSceneDetail, scene_id: "fresh-image-scene" };

    fireEvent.click(screen.getByRole("button", { name: /generate visual/i }));
    expect(await screen.findByText(/Generated a 1024×1024 image/)).toBeTruthy();

    const visualizeCall = callsTo("/visualize")[0];
    expect(visualizeCall).toBeTruthy();
    expect(requestBody(visualizeCall).render_mode).toBe("image");
  });

  it("loads a saved image scene without generating another image", async () => {
    window.localStorage.setItem(LAST_SCENE_KEY, "bus-tracking-demo");
    scenePayload = imageSceneDetail;
    assetsPayload = { items: [imageAsset], total: 1 };

    render(<App />);

    expect(
      await screen.findByText(/Restored bus-tracking-demo from revision 2/),
    ).toBeTruthy();
    await waitFor(() => {
      expect(document.querySelector(".image-host")).toBeTruthy();
    });

    expect(document.querySelector(".image-host")?.getAttribute("src")).toContain(imageAsset.url);
    expect(screen.getByRole("button", { name: /ai image/i }).getAttribute("aria-pressed")).toBe(
      "true",
    );
    expect(callsTo("/visualize")).toHaveLength(0);
    expect(callsTo("/update")).toHaveLength(0);
  });

  it("shows an unavailable placeholder for a missing image without regenerating it", async () => {
    window.localStorage.setItem(LAST_SCENE_KEY, "bus-tracking-demo");
    scenePayload = {
      ...imageSceneDetail,
      warnings: ["Image asset file is missing and is not regenerated automatically."],
    };
    assetsPayload = { items: [imageAsset], total: 1 };

    render(<App />);

    expect(await screen.findByText("Image unavailable")).toBeTruthy();
    expect(document.querySelector(".image-host")).toBeNull();
    expect(callsTo("/visualize")).toHaveLength(0);
    expect(callsTo("/update")).toHaveLength(0);
  });

  it("downloads the current image asset as a blob", async () => {
    window.localStorage.setItem(LAST_SCENE_KEY, "bus-tracking-demo");
    scenePayload = imageSceneDetail;
    assetsPayload = { items: [imageAsset], total: 1 };
    assetContentResponder = () => binaryResponse();

    const clickSpy = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => undefined);
    const createObjectURL = URL.createObjectURL as ReturnType<typeof vi.fn>;

    render(<App />);
    await screen.findByText(/Restored bus-tracking-demo from revision 2/);
    await waitFor(() => expect(document.querySelector(".image-host")).toBeTruthy());

    fireEvent.click(screen.getByRole("button", { name: /download image/i }));

    await waitFor(() => expect(createObjectURL).toHaveBeenCalled());
    expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining(imageAsset.url));
    expect(clickSpy).toHaveBeenCalled();
  });

  it.each([
    [503, "No image provider is configured. SVG generation is fully functional."],
    [429, "The image provider rate limit was exceeded."],
    [504, "The image provider timed out."],
    [422, "The image prompt was rejected by the provider content policy."],
  ])("shows a safe message for provider HTTP %i", async (status, detail) => {
    window.localStorage.setItem(LAST_SCENE_KEY, "bus-tracking-demo");
    updateResponder = () => jsonResponse({ detail }, status);

    render(<App />);
    await screen.findByText(/Restored bus-tracking-demo from revision 1/);

    fireEvent.click(screen.getByRole("button", { name: /ai image/i }));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain(detail);
    expect(alert.textContent).not.toMatch(/sk-|OPENAI_API_KEY=|GEMINI_API_KEY=/);

    // A failed switch keeps the previously saved SVG mode.
    expect(screen.getByRole("button", { name: /svg diagram/i }).getAttribute("aria-pressed")).toBe(
      "true",
    );
    expect(document.querySelector(".diagram-host svg")).toBeTruthy();
  });
});
