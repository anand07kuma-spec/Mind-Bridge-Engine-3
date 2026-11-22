# Mind Bridge · Engine 3 — Visualization Engine

Engine 3 turns a **structured visual instruction** into a rendered visual asset.
It never listens to speech, parses intent, or decides *what* to show. Engine 2
(or any producer) decides that and hands Engine 3 a validated contract.

```
Engine 2 (intent → structure)          Engine 3 (structure → visual)
┌──────────────────────────┐          ┌───────────────────────────────────────────┐
│ speech / text / context  │          │ validate → plan layout → render SVG →     │
│  ↓ produces              │  JSON    │ store in Visual Asset Memory → return     │
│ VisualInstruction ───────┼─────────▶│ scene_id + revision + inline SVG + asset  │
└──────────────────────────┘          └───────────────────────────────────────────┘
```

## 1. Pipeline

Every `POST /api/v1/visualize` call walks the same deterministic path:

1. **Validate** — Pydantic (`backend/app/schemas/scene.py`) rejects duplicate
   node ids, edges pointing at unknown nodes, self-loops, duplicate edge ids and
   unknown fields before any work happens.
2. **Plan** — `generators/diagram_generator.py` assigns layers (longest path from
   sources, with a cycle guard), orders nodes within layers by barycenter, and
   produces absolute `NodeBox` positions plus canvas size. If *every* node
   supplies `position`, auto-layout is skipped.
3. **Render** — `generators/svg_generator.py` emits a self-contained SVG:
   `<symbol>` icon set, arrow marker, shadow filter, cubic-bezier edges, label
   pills, escaped/wrapped text, fixed `viewBox` so the frontend can scale it.
4. **Remember** — `services/scene_service.py` computes a visual fingerprint and
   looks for an exact match in Visual Asset Memory. A new visual is rendered and
   its files are staged; an existing asset is reused. The asset row insert (or
   adoption) and the `reuse_count` touch happen later, inside the revision
   transaction.
5. **Record** — the instruction and resulting `asset_id` are written as a new
   row in `scene_revisions`; `scenes.current_revision` is bumped. Revision
   allocation, the optional `expected_revision` guard and the asset commit all run
   inside a single `BEGIN IMMEDIATE` transaction, so concurrent requests cannot
   collide. When the instruction and asset are unchanged, the current revision is
   returned and no row is inserted (visualize is idempotent).

## 2. API contract for Engine 2

Base URL: `http://localhost:8000/api/v1`. The path prefix is configurable with
`MIND_BRIDGE_API_PREFIX` (default `/api/v1`); paths below are relative to it.
CORS origins are configurable with `MIND_BRIDGE_CORS_ORIGINS`.

| Method | Path | Purpose |
| ------ | ---- | ------- |
| `POST` | `/visualize` | Render a full `VisualInstruction`, returns `VisualizeResponse`; optional `?expected_revision=` guard |
| `POST` | `/scenes/{scene_id}/update` | Apply an incremental `SceneUpdate` |
| `GET`  | `/scenes/{scene_id}` | Latest scene state + revisions + current SVG |
| `GET`  | `/assets` | List Visual Asset Memory (`?kind=&q=&limit=&offset=`) |
| `GET`  | `/assets/{asset_id}` | Asset metadata |
| `GET`  | `/assets/{asset_id}/content` | Raw SVG / image bytes (immutable cache headers) |
| `GET`  | `/assets/{asset_id}/thumbnail` | Thumbnail bytes (SVGs reuse the vector) |
| `GET`  | `/samples/visual-instruction` | Bundled bus-tracking demo |
| `GET`  | `/health` | `{status, engine, version, database, storage, timestamp, schema_version, schema_versions}` |

`GET /` (outside the prefix, not shown in OpenAPI) returns discovery metadata:
`api_prefix`, `schema_version`, `schema_versions` and `max_request_bytes`.

Interactive OpenAPI docs: `http://localhost:8000/docs`. The Engine 2-facing
walkthrough with captured request/response examples is
[`docs/ENGINE2_HANDOFF.md`](ENGINE2_HANDOFF.md).

Every HTTP response carries an `X-Request-ID` header. A well-formed inbound
`X-Request-ID` (`A-Z a-z 0-9 . _ -`, 1–128 chars) is preserved; otherwise one is
generated. Include it when reporting a failure — structured logs use the same id.

### 2.1 Request — `VisualInstruction`

**Schema version policy is strict.** Engine 2 must always send
`"schema_version": "1.0"` explicitly. Engine 3 currently defaults an omitted field
to `"1.0"` for hand-written payloads, but that default is a convenience, not part
of the integration contract. Any value other than a version in
`SUPPORTED_SCHEMA_VERSIONS` is rejected with `422` and the error lists the
supported versions. Engine 3 echoes the accepted version in `VisualizeResponse`,
`SceneDetail` and `GET /health`; scene updates keep the version that created the
scene, so version information survives every frontend/backend round trip.

```json
{
  "schema_version": "1.0",
  "render_mode": "svg",
  "scene_id": "bus-tracking-demo",
  "type": "diagram",
  "title": "Live Bus Tracking System",
  "description": "Optional 400-char summary shown under the title.",
  "options": { "direction": "LR", "show_edge_labels": true },
  "nodes": [
    { "id": "bus-1", "label": "Bus 101", "kind": "bus", "group": "fleet",
      "position": null, "metadata": { "route": "14A" } },
    { "id": "gps-server", "label": "GPS Server", "kind": "server" }
  ],
  "edges": [
    { "id": "bus1-gps", "source": "bus-1", "target": "gps-server",
      "label": "GPS ping", "style": "solid", "show_arrow": true }
  ]
}
```

Rules enforced by Pydantic:

- `render_mode` ∈ `svg | image` and defaults to `svg`. Image mode is **explicit**:
  it is never inferred from provider configuration or API-key presence. Image
  mode without a configured provider returns `503`; it never falls back to SVG.
- `scene_id`, node `id` and explicit edge `id` must match `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`.
- `kind` ∈ `bus | server | passenger | phone | database | cloud | service | sensor | generic`.
- `direction` ∈ `LR | TB`; `style` ∈ `solid | dashed`.
- `metadata` and `group` are free-form and **never rendered**; `metadata` is also
  excluded from the asset fingerprint so cosmetic metadata does not force a
  rerender. It still advances the scene revision — see 2.8.
- Edge `id` may be omitted; it defaults to `<source>-><target>`. Give parallel
  edges explicit ids.
- 1–80 nodes, up to 160 edges; titles ≤120 chars, descriptions ≤400, node labels
  ≤80, edge labels ≤48.
- `metadata` must be JSON-serializable, at most **5 levels deep** and at most
  **4 KiB** when serialized (configurable, see 2.7).

### 2.2 Input limits

Limits are enforced before rendering and return standard FastAPI errors:

| Limit | Default | Environment variable | Failure |
| ----- | ------- | -------------------- | ------- |
| Whole request body | 1 MiB (1 048 576 bytes) | `MIND_BRIDGE_MAX_REQUEST_BYTES` | `413` with `{"detail": "Request body exceeds the N-byte limit."}` |
| `metadata` nesting depth | 5 | `MIND_BRIDGE_METADATA_MAX_DEPTH` | `422` |
| `metadata` serialized size | 4 KiB | `MIND_BRIDGE_METADATA_MAX_BYTES` | `422` |

The body limit is enforced by `app/middleware.py` for both declared
`Content-Length` and chunked bodies; the graph rules above (duplicates, unknown
endpoints, self-loops, parallel-edge ids) are unchanged.

### 2.3 Display limits

Text is measured in **display units**, not code points: a narrow Latin glyph is
1 unit, CJK and emoji clusters are 2 units, combining marks are 0. Grapheme
clusters (flags, ZWJ emoji families, skin-tone modifiers, keycaps) are never
split across lines.

| Element | Input cap | Rendered budget | Behaviour when exceeded |
| ------- | --------- | --------------- | ----------------------- |
| Scene title | 120 chars | one line, ~62 units at the 920 px minimum canvas | truncated with `…` + warning |
| Description | 400 chars | one line, ~130 units at the minimum canvas | truncated with `…` + warning |
| Node label | 80 chars | up to 2 lines, ~17 units per line | wrapped; final line gets `…` + warning naming the node ids |
| Edge label | 48 chars | one line pill | never silently truncated; moved along its curve, or stacked across the corridor for parallel edges |

Every truncation is reported in `warnings`, including on cache hits, so a
shortened label is never silent. A single edge-label pill tries positions
`t = 0.5, 0.38, 0.62, 0.26, 0.74` along the cubic curve. Labels that share the
same `(source, target)` pair are treated as one parallel group: each label keeps
its position along the flow axis and the group is stacked perpendicular to the
corridor (vertically for `LR`, horizontally for `TB`), with a whole-group shift
when a card or the canvas edge is in the way. If even the least-bad stack cannot
clear the canvas, every label is still rendered and a warning explains that the
labels could not be placed inside the canvas; if labels still collide, the
existing overlap warning is emitted. Reduce parallel connections or disable edge
labels for very dense groups.

### 2.4 Response — `VisualizeResponse`

```json
{
  "scene_id": "bus-tracking-demo",
  "schema_version": "1.0",
  "render_mode": "svg",
  "revision": 1,
  "cache_hit": false,
  "asset": {
    "asset_id": "ast_fe2c04f284bd42f5",
    "kind": "svg",
    "content_hash": "e1c1bbe959e00fbadafd6200a416813c8c37181776bfeb271c0b05da70415109",
    "mime_type": "image/svg+xml",
    "width": 920,
    "height": 516,
    "category": "diagram",
    "tags": ["bus", "diagram", "passenger", "server", "svg"],
    "provider": "engine3-svg",
    "reuse_count": 0,
    "url": "/api/v1/assets/ast_fe2c04f284bd42f5/content",
    "thumbnail_url": "/api/v1/assets/ast_fe2c04f284bd42f5/thumbnail",
    "created_at": "2026-11-19T17:34:10Z",
    "last_used_at": "2026-11-19T17:34:10Z"
  },
  "svg": "<svg … 10,355 chars …>",
  "warnings": []
}
```

`asset` is the complete `AssetSummary`; ids, hashes and timestamps are
run-specific and `reuse_count` rises on exact-match reuse of a successful write.
`cache_hit: true` means
an identical visual already existed and no new asset was written, but it does
**not** mean the revision was unchanged: a metadata/group-only edit creates a new
revision with the same asset (2.8). `svg` is an inline, server-generated,
XML-escaped string — safe to inject into a sandboxed host element. `warnings`
reports layout issues such as cycles in the graph and every display truncation
(2.3); it is deterministic and returned on cache hits too (a cache hit does not
hide warnings). Repeating a byte-identical request returns the current revision
instead of creating a new one and reuses the asset. `schema_version` is the
version accepted from the instruction and is preserved across updates.

`render_mode` echoes the mode used for the revision. In `image` mode `svg` is
`null`; the raster bytes are served from `asset.url` (`/assets/{id}/content`)
and the thumbnail endpoint falls back to the full image when no separate
thumbnail was generated. Image assets are fingerprinted by provider, model,
configured size/quality, prompt template version, prompt hash and visual
identity, so changing any of those creates a new asset instead of reusing an
image generated under different conditions.

Every SVG id is namespaced (`mb-<scene>-<hash>-…`) and all `url(#…)` / `href="#…"`
references use the same prefix, so multiple SVGs can share one page. Scenes that
share an exact-match asset are re-prefixed per scene on read.

`POST /visualize` accepts an optional `expected_revision` query parameter. `0`
is create-only; a positive value must match the current revision. When omitted,
`/visualize` keeps the legacy last-write-wins behavior. The guard is checked
inside the revision-allocation transaction, so a stale request returns `409`
without writing a scene revision. The parameter is query-only: a body field is
rejected because `VisualInstruction` forbids unknown fields.

Asset lookup and rendering happen before the guard, but a newly rendered asset is
only staged on disk; its row is inserted inside the revision transaction, after
the guard passes. A reused asset is touched in that same transaction. A `409`
therefore writes no revision, creates no asset and does not increment
`reuse_count`; staged files are discarded. A missing stored asset may still be
repaired (file restored) before the guard, but its reuse count is not incremented.

### 2.5 Incremental updates — `SceneUpdate`

```json
{
  "expected_revision": 2,
  "render_mode": "image",
  "title": "Optional new title",
  "nodes_to_add":    [{ "id": "eta-engine", "label": "ETA Engine", "kind": "service" }],
  "nodes_to_update": [{ "id": "passenger", "label": "Passenger App · Live" }],
  "nodes_to_remove": ["old-node"],
  "edges_to_add":    [{ "source": "gps-server", "target": "eta-engine", "label": "Positions" }],
  "edges_to_remove": ["bus1-gps"]
}
```

The patch is applied in a fixed, documented order: edge removals → node removals
(connected edges are dropped) → node updates → node additions → edge additions.
The merged instruction is re-validated, re-rendered, stored as a new revision and
returned as a `VisualizeResponse`. A patch that changes nothing (for example `{}`)
returns the current revision without inserting history.

`nodes_to_update` entries are **partial**:

- Only fields present in the JSON are applied; omitted fields keep their current
  values.
- `label` and `kind` must be non-null when supplied; `null` is ignored (they
  cannot be cleared).
- `group` and `position` are set to the supplied value, including `null`, which
  clears them.
- `metadata` is **replaced wholesale**, never deep-merged. Send `{}` to clear it;
  `null` is ignored.
- Updating an id that is not in the scene (including one removed in the same
  patch) fails with `422`.

At the top level, `title` and `options` are applied only when non-null;
`description` is set from the supplied value, and `null` clears it.
`render_mode` is optional: omitting it keeps the scene's stored mode, while
supplying `svg` or `image` switches the mode for the resulting revision.

Edges have no partial-update operation. Replace an edge by listing its id in
`edges_to_remove` and adding the new definition in `edges_to_add` in the same
patch. Removing an unknown edge or node id, or adding an edge id that already
exists, fails with `422`.

Per-request operation caps: 40 each for `nodes_to_add`, `nodes_to_update` and
`nodes_to_remove`; 80 each for `edges_to_add` and `edges_to_remove`.

`expected_revision` is an optional optimistic-concurrency guard. If the scene has
moved past it, the request fails with `409 Conflict` and no scene revision is
written. Unknown scene → `404`; an invalid merged scene → `422`; a missing asset
file that cannot be regenerated → `503`.

### 2.6 Observability

The backend logs one JSON object per line to stdout (`app/observability.py`).
Every record carries `timestamp`, `level`, `logger`, `event` and `request_id`;
access and scene events add structured fields:

- `http.request` / `http.error`: `method`, `path`, `status`, `duration_ms`;
- `http.unhandled`: the same fields plus the exception traceback (status 500);
- `scene.visualized`, `scene.updated`, `scene.fetched`: `scene_id`, `revision`,
  and for writes `cache_hit` and `warning_count`.

Request bodies, instruction text, titles, labels, metadata, API keys and other
user content are **never logged**. Request ids are generated or echoed through
`X-Request-ID` (see section 2). Uvicorn's duplicate plain-text access log is
disabled; request events come from the middleware.

### 2.7 Configuration and remote access

| Setting | Backend variable | Default | Frontend counterpart |
| ------- | ---------------- | ------- | -------------------- |
| API prefix | `MIND_BRIDGE_API_PREFIX` | `/api/v1` | `VITE_API_PREFIX` |
| API origin | — | `http://localhost:8000` (served) | `VITE_API_BASE_URL` |
| CORS origins | `MIND_BRIDGE_CORS_ORIGINS` | localhost:5173/4173 | — |
| Request body cap | `MIND_BRIDGE_MAX_REQUEST_BYTES` | 1 MiB | — |

`VITE_API_BASE` is still read as a deprecated alias when `VITE_API_BASE_URL` is
absent; new deployments should set `VITE_API_BASE_URL` only. Keep the frontend
prefix identical to the backend prefix — the client composes every path (and
`/docs`) from these two values.

Asset `url` / `thumbnail_url` values are **prefix-relative** (for example
`/api/v1/assets/ast_…/content`). Resolve them against the Engine 3 origin, not
against the page origin. For a remote Engine 2 deployment:

1. make Engine 3 reachable from Engine 2 and add Engine 2's origin to
   `MIND_BRIDGE_CORS_ORIGINS` (no wildcard with credentials is used);
2. if the prefix differs from `/api/v1`, set both `MIND_BRIDGE_API_PREFIX` and
   `VITE_API_PREFIX` to the same value;
3. prefer the inline `svg` field when embedding; use asset URLs only for
   downloads, thumbnails or `<img>` rendering, resolving them against the API
   origin;
4. there is no authentication yet — expose Engine 3 only on a trusted network.

### 2.8 Revision semantics and polling

Revision numbers are per scene, monotonic and immutable; history is never
rewritten. What counts as a change is defined by the scene state, not by pixels:

| Change | Revision | Asset | `cache_hit` |
| ------ | -------- | ----- | ----------- |
| Byte-identical instruction | unchanged | same `asset_id` | `true` |
| `metadata`/`group` only | **+1** | same `asset_id` | `true` |
| Rendered field changed | +1 | new or reused asset | `true`/`false` |

Metadata and `group` are excluded from the asset fingerprint (section 3) but are
part of the scene instruction, so a metadata-only edit is persisted as a new
revision. This applies to `/visualize` as well as updates.

For `/visualize`, send `expected_revision=0` to create a scene only, or the last
known revision to replace an existing scene. A stale value returns `409` and
creates no revision; an omitted value is the legacy unguarded path. On `409`,
re-fetch the scene, reconcile against the fresh state, and retry with the new
revision.

Engine 2 must always persist the `revision` returned by the last successful call.
Never infer "nothing changed" from `cache_hit: true` or from a stable `asset_id` —
both are normal for metadata-only edits.

Milestone 1 is poll-only:

- No WebSocket or SSE transport is available.
- `GET /scenes/{scene_id}` has no `ETag`/`If-None-Match` support and always
  returns the complete scene plus the full revision history (newest first, for
  example `[3, 2, 1]`), so response size grows with edit history.
- Recommended client policy: poll every 3–5 seconds only while a scene is open;
  reuse the response from your own successful update instead of immediately
  issuing another `GET`; stop when the scene closes; back off on errors.

## 3. Visual Asset Memory

- **Storage:** files in `assets/svg/`, `assets/images/`, `assets/thumbnails/`;
  metadata in SQLite (`data/mindbridge.db`).
- **Ids:** `ast_<16 hex>`; every asset gets an immutable id.
- **Exact matching:** SHA-256 over a canonical JSON *visual fingerprint*
  (generator name@version, type, title, description, options, nodes
  (id/label/kind/position), edges). `schema_version`, `scene_id`, `group` and
  `metadata` are excluded so the same diagram can be shared across scenes.
- **Reuse:** a hit increments `reuse_count` and updates `last_used_at` after the
  scene guard passes; no file is rewritten. A rejected write does not touch the
  asset.
- **Similarity search:** intentionally not implemented yet. The unique index
  `(content_hash, kind, provider)` and the `content_hash` column are the seam for
  a future vector index or perceptual hash.
- **Categories/tags:** columns exist (`category`, `tags_json`); the current SVG
  provider writes `category="diagram"`. `GET /assets?q=` filters over tags,
  category and metadata.

SQLite schema: `scenes` (latest instruction + revision counter), `scene_revisions`
(immutable history `(scene_id, revision)` → instruction + asset), `assets`
(metadata + file paths). WAL mode, foreign keys, one short-lived connection per
unit of work.

## 4. Frontend workspace

`frontend/src/App.tsx` is the workspace shell:

- instruction JSON editor prefilled from the sample; **Load sample**, **Generate visual**;
- pipeline strip mirroring validate → plan → render → remember;
- scene state card (revision, asset id, cache vs fresh, size) and **Simulate scene update**,
  which demonstrates the patch API (adds `ETA Engine`, then toggles the passenger label);
- canvas with the **actual inline SVG** (`dangerouslySetInnerHTML` of server-rendered,
  escaped markup), zoom, fit-to-canvas, download of the current revision;
- Visual Asset Memory list with thumbnails, current badge and reuse counts;
- reserved **Image preview** area (backend image assets exist after Milestone 1;
  the frontend preview is wired in Milestone 2).

`services/config.ts` resolves `VITE_API_BASE_URL` (canonical) with the deprecated
`VITE_API_BASE` alias, and `VITE_API_PREFIX` (default `/api/v1`).
`services/api.ts` is the single fetch layer (typed, prefix-aware, error
normalisation, sends a client-generated `X-Request-ID`). Rendering is
intentionally not re-implemented on the client: the SVG (or the image asset URL)
is the artifact returned by Engine 3.

## 5. Image generation pipeline (explicit opt-in)

`backend/app/image_providers/` contains:

- `base.py` — `ImageGenerationRequest` / `GeneratedImage` /
  `ProviderCapabilities`, the `ImageProvider` ABC, the safe provider error
  taxonomy, and `UnavailableImageProvider`;
- `factory.py` — selects exactly one provider from
  `MIND_BRIDGE_IMAGE_PROVIDER` (`none`, `openai`, `gemini`) and performs no
  network I/O;
- `http.py` — shared injectable HTTP transport, response-size guard and
  `Retry-After` parsing used by both adapters;
- `prompt.py` — deterministic prompt construction from structured scene fields,
  with untrusted-text sanitisation, a configurable character limit, a SHA-256
  prompt hash, and metadata that never contains the full prompt;
- `validation.py` — explicit PNG/JPEG/WebP allowlist, MIME/signature agreement,
  byte-size limits, and standard-library dimension parsing (no Pillow);
- `openai.py` — OpenAI Images API adapter with an injectable transport,
  base64-only responses, no URL downloads, no automatic retries, and safe error
  mapping;
- `gemini.py` — Google Gemini `generateContent` adapter with an injectable
  transport, the API key in the `x-goog-api-key` header (never the URL), inline
  image data only, strict base64 decoding, configured aspect-ratio/image-size
  mapping, no URL downloads, no automatic retries, and safe error mapping.

`MIND_BRIDGE_IMAGE_PROVIDER` defaults to `none`. OpenAI is constructed when
`OPENAI_API_KEY` is set; Gemini is constructed when `GEMINI_API_KEY` is set. A
missing key returns an unavailable provider instead of breaking startup. Provider
construction performs no network calls.

`SceneService` invokes the configured provider **only** when a request carries
`"render_mode": "image"`. SVG remains the default and never touches the provider.
Image assets use the existing `AssetManager` `kind="image"` path and the same
`BEGIN IMMEDIATE` / `expected_revision` / exact-match-adoption transaction as SVG.
Missing image files are **never** regenerated automatically: reads return `503`
(or a scene warning) and the caller must explicitly request a new image.

Provider failures map to safe HTTP responses: not configured/unavailable `503`,
auth `502`, rate limit `429` (with `Retry-After`), timeout `504`, content policy
`422`, malformed response `502`, configuration `500`. Messages never include API
keys, prompts or raw provider bodies.

An in-process single-flight lock coalesces identical image fingerprints so one
worker does not pay twice for the same visual. **Limitation:** the lock is
in-memory only. Multiple uvicorn workers, multiple hosts, or a process restart do
not share it, so cross-process races can still issue duplicate provider calls.
Correctness is unaffected — `_persist_scene` adopts the first committed asset and
discards the loser's staged files — but duplicate spend is possible. Run a single
worker, or add an external lock, if cost control across workers is required.

## 6. Module map

```
backend/app/
  main.py                  app factory, middleware wiring, logging setup
  config.py                env-driven Settings, directory bootstrap
  limits.py                metadata depth/size limits (env-driven)
  middleware.py            request ids, access logging, body-size limit
  observability.py         JSON logging + request-id context
  api/routes.py            HTTP endpoints + scene operation logs
  schemas/scene.py         the Engine 2 ↔ Engine 3 contract
  generators/
    diagram_generator.py   layered layout planner
    svg_generator.py       deterministic SVG renderer, display planning
    text_metrics.py        grapheme-aware width, wrapping, truncation
  services/
    scene_service.py       visualize / update / fetch orchestration
    asset_manager.py       fingerprinting, storage, reuse, listing
  storage/database.py      SQLite schema + sessions
  image_providers/         base, factory, http, prompt, validation, OpenAI + Gemini adapters
tests/                     schemas, generator, limits, observability, API tests
```

## 7. Known limits

- `type` supports diagrams only. `render_mode` selects deterministic SVG (default)
  or one AI-generated raster image; image mode requires a configured provider and
  returns `503` otherwise. There is no automatic SVG fallback.
- Image mode is explicit and never inferred from API-key presence. Missing image
  files are never regenerated automatically. The in-process single-flight lock
  does not coordinate multiple workers/hosts, so duplicate paid calls are
  possible across processes (see 5).
- Layout handles DAGs well; cycles render with a warning (edges may overlap).
- Exact-match reuse only — no perceptual/semantic dedupe yet.
- No auth, rate limiting or multi-user isolation; intended to run locally/trusted.
- Real-time delivery (websockets/SSE) is not implemented; consumers poll or refresh
  (recommended policy in 2.8).
- No conditional GET: `GET /scenes/{id}` always returns the complete scene and
  revision history (no `ETag`/`If-None-Match`).
- Display width is an approximation (1 Latin / 2 CJK-emoji / 0 combining units);
  unusual fonts can still differ slightly from the planned width. Very long edge
  labels may extend past the canvas near its edges. A parallel label group that
  cannot fit inside the canvas keeps every label and emits a canvas/overlap
  warning rather than dropping labels.
- The body limit protects parsing; there is no per-client rate limit yet.
- On Windows machines with Smart App Control, `pydantic>=2.12` DLLs are blocked;
  the backend pins `pydantic<2.12` (see `backend/requirements.txt`).
- Verified dependency line: FastAPI 0.165 requires Starlette ≥1.6 (tested with
  1.10); Starlette 1.x TestClient uses `httpx2` and deprecates `httpx`, so
  `requirements-dev.txt` installs `httpx2`.
