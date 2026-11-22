# Engine 3 — Final Production-Readiness Report

**Phase 4, Milestone 3 (final audit).** Date: 2026-11-22.
**Scope:** Engine 3 only. Engine 1 and Engine 2 were not modified.

This report describes what is implemented and verified in this workspace. It does
**not** claim a production deployment, and it does **not** claim a successful live
OpenAI or Gemini generation: no real provider call was made.

---

## 1. Architecture summary

Engine 3 is a standalone FastAPI service plus a React reference workspace.

```
Engine 2 ── VisualInstruction (JSON) ──► FastAPI API
                                          │
                                          ├─ schema validation (Pydantic, schema 1.0)
                                          ├─ SVG path: planner → layout → router → generator
                                          ├─ image path (explicit only): prompt builder
                                          │    → OpenAI or Gemini adapter → validated raster
                                          ├─ Visual Asset Memory
                                          │    (SQLite WAL + content-addressed files)
                                          └─ scene revisions (monotonic, immutable)
                                                   │
                          SVG / image asset URLs ◄─┘
                                                   │
                                          React workspace (reference consumer)
```

- **Backend:** Python 3.12, FastAPI, Pydantic 2, Starlette, Uvicorn.
- **Rendering:** deterministic SVG generator with layered layout, obstacle-aware
  edge routing and grapheme-aware text metrics.
- **Image providers:** OpenAI Images and Google Gemini adapters, injectable HTTP
  transports, no automatic retries, safe error taxonomy.
- **Storage:** SQLite in WAL mode for scenes/revisions/assets, filesystem for
  `assets/svg`, `assets/images`, `assets/thumbnails`.
- **Frontend:** React 18 + TypeScript, Vite, Vitest + Testing Library.

The critical write path (`_persist_scene`) uses `BEGIN IMMEDIATE`, an
`expected_revision` guard, exact-match asset adoption and staged-file cleanup.
That transaction behaviour was preserved unchanged in this milestone.

---

## 2. Implemented capabilities

### SVG (default, deterministic)

- Strict `schema_version: "1.0"` validation with actionable `422` errors.
- 1–80 nodes, ≤160 edges, 1 MiB body limit, metadata depth/size limits.
- Auto-layout `LR`/`TB`, manual positions, cycle warnings.
- Self-contained, escaped, per-scene namespaced SVG.
- Display hardening: overflow warnings, edge-label collision avoidance, parallel
  edge stacking.
- Content-addressed asset reuse; `reuse_count` on successful writes.
- Revision history and optimistic concurrency.

### Explicit AI image mode

- `render_mode: "svg" | "image"` on the instruction; default `"svg"`.
- Image mode is never inferred from provider/key presence.
- OpenAI (`gpt-image-1`) and Gemini (`gemini-2.5-flash-image`) adapters.
- Exactly one provider active at a time, selected by
  `MIND_BRIDGE_IMAGE_PROVIDER`.
- PNG/JPEG/WebP allowlist, MIME/signature agreement, byte-size limits,
  standard-library dimension parsing.
- Deterministic prompt construction from structured fields; metadata stores a
  prompt hash, never the prompt.
- Image fingerprint includes provider, model, configured size/quality, prompt
  template version, prompt hash and full visual identity.
- Exact-match reuse across scenes; in-process single-flight for identical work.
- Missing image files are never regenerated automatically.
- Safe provider error mapping (no keys, prompts or raw bodies).

### Scene lifecycle

- Create with `expected_revision=0` (create-only).
- Guarded replacement with a positive `expected_revision`.
- Partial updates: title/description/options, node add/update/remove, edge
  add/remove, `render_mode` preservation and switching.
- Byte-identical requests keep the revision; changed instructions increment it.
- `GET /scenes/{id}` restores state without generating anything.
- Asset content and thumbnails served with immutable cache headers.
- Read-only asset GC report (`GET /assets/gc`) never deletes.

### Frontend

- SVG / AI image mode control; SVG default.
- SVG rendered inline; raster images rendered through `<img>`.
- Saved-scene restoration without a new revision or provider call.
- Mode switches use `expected_revision`.
- Image download via blob with direct-URL fallback.
- Current-image preview and stored-image grid.
- Safe provider-error messages; failed mode switches revert.

---

## 3. Test and static-check results

Final verification run in this workspace:

| Check | Command | Result |
| --- | --- | --- |
| Backend tests | `pytest -q` | **383 passed** |
| Backend lint | `ruff check app tests` | **All checks passed** |
| Backend types | `mypy` | **Success: no issues in 28 source files** |
| Frontend tests | `vitest run` | **23 passed** (2 files) |
| Frontend lint | `eslint .` | **Clean** |
| TypeScript | `tsc --noEmit` | **Clean** |
| Production build | `vite build` | **Success** (32 modules) |

Baseline before this milestone was 382 backend / 23 frontend tests. This
milestone adds one focused API integration test: a stale image-scene update
returns `409`, does not call the provider, and leaves the current asset intact.

### Integration coverage relevant to handoff

Verified with temporary databases and asset directories and deterministic fakes:

- Engine 2-style SVG payload with no `render_mode` renders as SVG.
- Explicit `render_mode: "image"` returns an image `asset.url`.
- Saved image scenes restore via `GET` with no provider call.
- Stale/guarded revision conflicts do not create, delete or corrupt assets.
- Missing image files return `503`/warnings and never regenerate.
- Provider errors map to `422`/`429`/`502`/`503`/`504` with safe messages.
- No test performs a real OpenAI or Gemini call and no test requires an API key.

---

## 4. API handoff document

**`docs/ENGINE2_ENGINE3_INTEGRATION.md`**

It contains the exact request/response JSON, update semantics, asset retrieval,
error table, polling policy, provider configuration, copy-paste curl/Python
examples and integration limitations.

The earlier SVG-only guide `docs/ENGINE2_HANDOFF.md` remains valid for the core
contract. Where the two differ on image mode, the new document is authoritative.

---

## 5. Provider setup requirements

Image generation is optional. SVG works with no provider.

```bash
# Default: images disabled, SVG fully functional
MIND_BRIDGE_IMAGE_PROVIDER=none

# OpenAI
MIND_BRIDGE_IMAGE_PROVIDER=openai
OPENAI_API_KEY=...                  # backend-only; never sent to clients
# optional: MIND_BRIDGE_IMAGE_MODEL=gpt-image-1

# Gemini
MIND_BRIDGE_IMAGE_PROVIDER=gemini
GEMINI_API_KEY=...                  # backend-only; never sent to clients
# optional: MIND_BRIDGE_IMAGE_MODEL=gemini-2.5-flash-image
```

Other image settings: `MIND_BRIDGE_IMAGE_SIZE` (default `1024x1024`),
`MIND_BRIDGE_IMAGE_QUALITY` (`standard`), `MIND_BRIDGE_IMAGE_TIMEOUT_SECONDS`
(`60`), `MIND_BRIDGE_IMAGE_MAX_RETRIES` (`0`), `MIND_BRIDGE_IMAGE_MAX_BYTES`
(20 MiB), `MIND_BRIDGE_IMAGE_PROMPT_MAX_CHARS` (`4000`).

Operational notes:

- Only one provider is constructed; there is no fallback chain.
- A missing key produces an unavailable provider, not a startup failure.
- Provider construction performs no network I/O.
- Real generation is a paid action and is always triggered by an explicit
  `render_mode: "image"` request or update.

---

## 6. Security considerations

- **No authentication or authorization is implemented.** Deploy only on a
  trusted network or behind an authenticating gateway.
- **No multi-user isolation.** Scenes and assets are global to the instance.
- API keys are read only from the backend environment, stored with `repr=False`,
  and never logged or returned. Request/response bodies never carry keys.
- Provider error messages are constructed to exclude keys, prompts and raw
  provider bodies.
- Logs are structured JSON; instruction text, titles, labels and metadata are not
  logged. Every response carries `X-Request-ID`.
- SVG output escapes user text and is namespaced per scene. Raster images are
  served as files and must be rendered via `<img>`, not inlined.
- CORS is restricted to configured origins; credentials are disabled.
- Request bodies are size-limited (1 MiB default) before parsing.
- Asset content URLs are immutable and cacheable; asset ids are content-addressed.
- There is no rate limiting in Engine 3. Provider rate limits surface as `429`;
  API-level abuse protection is a deployment concern.

---

## 7. Known limitations

1. **Cross-process single-flight only.** Duplicate paid image calls are possible
   across workers/hosts. Storage stays correct via asset adoption. Run one worker
   or add an external lock for cost control.
2. **No push transport.** Poll-only; no WebSocket/SSE and no conditional GET.
   Scene detail grows with revision history.
3. **Image fingerprints are broad.** Title/label/group/position changes can
   trigger a new paid image.
4. **No automatic image regeneration.** A missing image file yields `503` or a
   warning until an explicit image request is made.
5. **No automatic provider retries** by default.
6. **Image thumbnails may equal the full image** (no image resizer).
7. **No similarity search.** Asset matching is exact content hash only.
8. **No authentication, rate limiting or multi-tenancy.**
9. **Schema `1.0` only.**
10. **No frontend provider preflight.** The UI surfaces the backend's safe `503`.

---

## 8. Deployment prerequisites

- Python 3.12 with `backend/requirements.txt` (runtime) or
  `requirements-dev.txt` (tests/checks).
- Node.js + npm for the frontend build (`frontend/package.json`).
- A writable assets directory and SQLite database path:
  - `MIND_BRIDGE_ASSETS_DIR` (default `<project>/assets`)
  - `MIND_BRIDGE_DB_PATH` (default `<project>/data/mindbridge.db`)
- Set `MIND_BRIDGE_CORS_ORIGINS` to the real frontend origins and keep
  `MIND_BRIDGE_API_PREFIX` aligned with the frontend `VITE_API_PREFIX`.
- Serve the backend with a production ASGI server. If image cost control matters,
  run a single worker or add an external lock.
- Put Engine 3 behind an authenticating reverse proxy; do not expose it publicly.
- Optional: set the provider variables from section 5 only if image generation is
  wanted.
- No database migration is required for this milestone; the schema is created on
  startup.
- No deployment was performed as part of this audit.

---

## 9. What has NOT been verified

- **No live OpenAI or Gemini call.** Adapters are covered with injectable fake
  transports; real credentials, billing, provider-side latency and content policy
  are untested.
- **No live Engine 2 integration.** Engine 1/Engine 2 source was not available in
  this workspace. Cross-engine integration remains **pending** and must be
  validated by the Engine 2 team against this contract.
- **No production deployment, load test or soak test.**
- **No multi-worker/multi-host concurrency test** for duplicate paid calls.
- **No browser screenshot verification** in this milestone because the desktop
  browser window was not visible; DOM/accessibility checks were used instead.
- **No security penetration test or auth review** (there is no auth yet).

---

## 10. Exact next action for the Engine 2 developer

1. Read `docs/ENGINE2_ENGINE3_INTEGRATION.md`.
2. Point a development Engine 2 at a local Engine 3 with
   `MIND_BRIDGE_IMAGE_PROVIDER=none`; verify SVG create/update/restore using
   `expected_revision` and `GET /scenes/{id}`.
3. Persist `scene_id` and the returned `revision`; treat `409` as
   reconcile-and-retry.
4. Resolve `asset.url`/`asset.thumbnail_url` against the Engine 3 origin.
5. Only after SVG passes, have the Engine 3 operator enable **one** provider and
   run a single explicitly approved paid image request; confirm the image asset
   URL renders and that reopening the scene does not regenerate it.
6. Report any contract mismatch to the Engine 3 team with the `X-Request-ID` from
   the failing response.

**Status:** Engine 3 is ready for integration testing with Engine 2 on a trusted
network. It is not certified for public production until authentication,
cross-process cost control and a live provider acceptance test are in place.
