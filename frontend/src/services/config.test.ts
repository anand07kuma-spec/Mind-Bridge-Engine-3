import { afterEach, describe, expect, it, vi } from "vitest";

import { normalizeApiPrefix, resolveApiBase } from "./config";

afterEach(() => {
  vi.unstubAllEnvs();
  vi.resetModules();
});

describe("resolveApiBase", () => {
  it("prefers the canonical VITE_API_BASE_URL", () => {
    expect(resolveApiBase("https://engine.example/", "https://legacy.example")).toBe(
      "https://engine.example",
    );
  });

  it("falls back to the deprecated VITE_API_BASE alias", () => {
    expect(resolveApiBase(undefined, "http://127.0.0.1:9000/")).toBe("http://127.0.0.1:9000");
  });

  it("defaults to localhost when neither variable is set", () => {
    expect(resolveApiBase(undefined, undefined)).toBe("http://localhost:8000");
  });

  it("treats blank values as unset", () => {
    expect(resolveApiBase("   ", "")).toBe("http://localhost:8000");
  });
});

describe("normalizeApiPrefix", () => {
  it("adds a leading slash and strips trailing slashes", () => {
    expect(normalizeApiPrefix("api/v1/")).toBe("/api/v1");
    expect(normalizeApiPrefix("///api/v2///")).toBe("/api/v2");
  });

  it("maps an empty or root prefix to an empty string", () => {
    expect(normalizeApiPrefix(undefined)).toBe("");
    expect(normalizeApiPrefix("")).toBe("");
    expect(normalizeApiPrefix("/")).toBe("");
  });
});

describe("apiPath", () => {
  it("combines the configured prefix with the endpoint path", async () => {
    vi.stubEnv("VITE_API_PREFIX", "custom/v9/");
    vi.resetModules();
    const config = await import("./config");
    expect(config.API_PREFIX).toBe("/custom/v9");
    expect(config.apiPath("/health")).toBe("/custom/v9/health");
    expect(config.apiPath("health")).toBe("/custom/v9/health");
  });
});

describe("module configuration", () => {
  it("reads both base URL and prefix at load time", async () => {
    vi.stubEnv("VITE_API_BASE_URL", "https://engine.example/");
    vi.stubEnv("VITE_API_PREFIX", "api/v3");
    vi.resetModules();
    const config = await import("./config");
    expect(config.API_BASE).toBe("https://engine.example");
    expect(config.API_PREFIX).toBe("/api/v3");
    expect(config.apiPath("/visualize")).toBe("/api/v3/visualize");
  });
});
