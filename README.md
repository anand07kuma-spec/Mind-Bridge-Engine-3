# Mind Bridge · Engine 3 — Visualization Engine

Standalone visualization engine for **Mind Bridge**. It receives a structured
**visual instruction** from Engine 2 and produces a rendered visual — deterministic
SVG by default, or one explicitly requested AI-generated image — stores it in
**Visual Asset Memory**, and serves it to the React workspace.

Engine 3 does not interpret speech or intent. The boundary is explicit: Engine 2
decides *what* to show, Engine 3 decides *how it looks* and remembers it.

**The JSON → SVG pipeline and the opt-in AI image pipeline are implemented and
tested end-to-end**, including the bus-tracking demo with 3 buses, a GPS server,
a passenger and labelled directional connections.

---

## Stack

| Layer | Technology |
| ----- | ---------- |
| Backend | Python 3.12, FastAPI 0.165, Starlette 1.10, Pydantic 2.11, Uvicorn |
| Rendering | Deterministic SVG generator + layered layout planner + obstacle-aware edge router + grapheme-aware text metrics |
| Storage | SQLite (WAL) + filesystem (`assets/svg`, `assets/images`, `assets/thumbnails`) |
| Frontend | React 18, TypeScript 5.6 (strict), Vite 6 |
| Tooling | Ruff, mypy, ESLint, GitHub Actions |
| Tests | pytest + Starlette TestClient via `httpx2` (383 tests) · Vitest + Testing Library (23 tests) |

---

## Project structure

```
Engine 3/
├── backend/
│   ├── app/
│   │   ├── main.py                  FastAPI factory, middleware wiring
│   │   ├── config.py                Settings (env overridable), dirs
│   │   ├── limits.py                body/metadata limits
│   │   ├── middleware.py            request ids, access logs, 413 guard
│   │   ├── observability.py         JSON logging + request-id context
│   │   ├── api/routes.py            all HTTP endpoints + scene logs
│   │   ├── schemas/scene.py         Engine 2 ↔ Engine 3 contract
│   │   ├── generators/
│   │   │   ├── diagram_generator.py layered layout planner
│   │   │   ├── edge_routing.py      obstacle-aware edge routing
│   │   │   ├── svg_generator.py     deterministic SVG renderer
│   │   │   └── text_metrics.py      grapheme-aware width/wrap/truncate
│   │   ├── services/
│   │   │   ├── scene_service.py     visualize / update / fetch
│   │   │   └── asset_manager.py     fingerprints, reuse, storage
│   │   ├── storage/database.py      SQLite schema + sessions
│   │   └── image_providers/         provider contract, prompt, validation, HTTP, OpenAI + Gemini
│   ├── tests/                       conftest + schema/generator/limits/API tests
│   ├── requirements.txt             runtime dependencies
│   ├── requirements-dev.txt         tests, Ruff, mypy, httpx2
│   ├── pyproject.toml               Ruff + mypy configuration
│   └── pytest.ini
├── frontend/
│   ├── src/
│   │   ├── components/VisualCanvas.tsx, Toolbar.tsx, icons.tsx
│   │   ├── services/api.ts          typed API client
│   │   ├── services/config.ts       API base/prefix resolution
│   │   ├── App.tsx                  workspace shell
│   │   ├── main.tsx, styles.css
│   │   └── App.test.tsx, services/config.test.ts
│   ├── eslint.config.js             ESLint flat config
│   ├── package.json, vite.config.ts, tsconfig.json
│   └── index.html
├── .github/workflows/ci.yml         backend + frontend CI
├── sample_data/bus_tracking.json    demo instruction
├── assets/{svg,images,thumbnails}/  generated assets
├── data/                            SQLite database (runtime)
├── docs/ARCHITECTURE.md             full contract + design
├── docs/ENGINE2_HANDOFF.md          Engine 2 integration guide (SVG core)
├── docs/ENGINE2_ENGINE3_INTEGRATION.md  SVG + AI image handoff contract
├── docs/ENGINE3_FINAL_READINESS.md  final audit + production readiness
└── .gitignore
```

---

## Running it

### Backend

```bash
cd backend
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -r requirements-dev.txt   # Windows
# source .venv/bin/activate && pip install -r requirements-dev.txt  # macOS/Linux

./.venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

`requirements-dev.txt` includes runtime dependencies plus pytest, Ruff, mypy and
`httpx2` (the Starlette 1.x TestClient transport). Use `requirements.txt` for a
runtime-only install.

- API: <http://localhost:8000>
- Swagger UI: <http://localhost:8000/docs>
- Health: <http://localhost:8000/api/v1/health>

Environment overrides (all optional):

| Variable | Default |
| -------- | ------- |
| `MIND_BRIDGE_ASSETS_DIR` | `<project>/assets` |
| `MIND_BRIDGE_DB_PATH` | `<project>/data/mindbridge.db` |
| `MIND_BRIDGE_CORS_ORIGINS` | `http://localhost:5173,http://127.0.0.1:5173,http://localhost:4173,http://127.0.0.1:4173` |
| `MIND_BRIDGE_API_PREFIX` | `/api/v1` |
| `MIND_BRIDGE_MAX_REQUEST_BYTES` | `1048576` (1 MiB) |
| `MIND_BRIDGE_METADATA_MAX_DEPTH` | `5` |
| `MIND_BRIDGE_METADATA_MAX_BYTES` | `4096` (4 KiB) |
| `MIND_BRIDGE_IMAGE_PROVIDER` | `none` (`none` / `openai` / `gemini`) |
| `OPENAI_API_KEY` | unset — backend-only, required for `openai` |
| `GEMINI_API_KEY` | unset — backend-only, required for `gemini` |
| `MIND_BRIDGE_IMAGE_MODEL` | `gpt-image-1` / `gemini-2.5-flash-image` |
| `MIND_BRIDGE_IMAGE_SIZE` | `1024x1024` |
| `MIND_BRIDGE_IMAGE_QUALITY` | `standard` |

Image generation is opt-in: SVG is always available, and an image is produced
only for an explicit `"render_mode": "image"` request. Exactly one provider is
active at a time; a missing key leaves image mode unavailable (`503`) without
affecting SVG. Never send provider keys from Engine 2 or the frontend.

Logs are JSON on stdout. Every response carries `X-Request-ID`; include it in bug
reports. Log records never contain instruction text, titles or metadata.

### Frontend

```bash
cd frontend
npm install
npm run dev          # http://localhost:5173
```

Open <http://localhost:5173>. On boot the workspace loads the bundled sample and
renders it automatically. The **Render mode** control switches between a
deterministic SVG diagram (default) and one explicit AI-generated image. Mode
switches on a saved scene are applied as a new revision using the scene's
`expected_revision`; opening or refreshing a saved scene never triggers image
generation. Image mode requires the backend provider to be configured
(`OPENAI_API_KEY` or `GEMINI_API_KEY`); without it, image requests return a safe
`503` and SVG rendering remains fully available.

`VITE_API_BASE_URL` points the client at a different backend (default
`http://localhost:8000`). `VITE_API_PREFIX` must match the backend
`MIND_BRIDGE_API_PREFIX` (default `/api/v1`). `VITE_API_BASE` remains as a
deprecated alias for older `.env` files.

Asset URLs in API responses are prefix-relative (`/api/v1/assets/…`); resolve
them against the Engine 3 origin, not the page origin.

### Tests, lint and build

```bash
# backend
cd backend
./.venv/Scripts/python.exe -m ruff check .
./.venv/Scripts/python.exe -m mypy app
./.venv/Scripts/python.exe -m pytest

# frontend
cd frontend
npm run lint
npm test
npm run build
```

Verified locally: **383 backend tests pass**, **23 frontend tests pass**, Ruff
reports no findings, mypy reports no issues in 28 source files, ESLint reports no
findings, and `tsc --noEmit && vite build` succeeds.

CI (`.github/workflows/ci.yml`) runs exactly these commands for every push and
pull request.

---

## API

| Method | Path | Purpose |
| ------ | ---- | ------- |
| `POST` | `/api/v1/visualize` | Render a full visual instruction |
| `POST` | `/api/v1/scenes/{scene_id}/update` | Apply an incremental patch, new revision |
| `GET`  | `/api/v1/scenes/{scene_id}` | Scene state, revision history, current SVG |
| `GET`  | `/api/v1/assets` | List Visual Asset Memory |
| `GET`  | `/api/v1/assets/{asset_id}` | Asset metadata |
| `GET`  | `/api/v1/assets/{asset_id}/content` | Raw SVG/image bytes |
| `GET`  | `/api/v1/assets/{asset_id}/thumbnail` | Thumbnail bytes |
| `GET`  | `/api/v1/samples/visual-instruction` | Demo instruction |
| `GET`  | `/api/v1/health` | Readiness probe + supported schema versions |

The prefix is configurable through `MIND_BRIDGE_API_PREFIX`. The full payload
contract, schema-version policy, input/display limits, logging contract,
fingerprinting and update ordering are documented in
**[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**.

Quick check:

```bash
curl -s http://localhost:8000/api/v1/health
curl -s -X POST http://localhost:8000/api/v1/visualize \
  -H "Content-Type: application/json" \
  --data-binary @sample_data/bus_tracking.json
```

---

## Engine 2 integration

The contract is finalized. Exact request/response examples, error bodies and the
polling policy are in **[docs/ENGINE2_HANDOFF.md](docs/ENGINE2_HANDOFF.md)**.
For the complete SVG **and** explicit AI image contract, use
**[docs/ENGINE2_ENGINE3_INTEGRATION.md](docs/ENGINE2_ENGINE3_INTEGRATION.md)**.
The final audit and production-readiness notes are in
**[docs/ENGINE3_FINAL_READINESS.md](docs/ENGINE3_FINAL_READINESS.md)**.

1. **Schema version is explicit.** Engine 2 must always send
   `"schema_version": "1.0"`. Any other value returns `422`; the accepted version
   is echoed on every response. Do not rely on Engine 3's omission default.
2. **Visualize.** Engine 2 builds a `VisualInstruction` (schema in
   `backend/app/schemas/scene.py`) and `POST`s it to `/api/v1/visualize`. The
   response carries `scene_id`, `schema_version`, `revision`, `cache_hit`, full
   asset metadata and an inline `svg`. Inject the SVG directly; resolve
   `asset.url` against the Engine 3 origin. Add the optional
   `?expected_revision=` query parameter to guard the write: `0` means create a
   new scene, and the current revision means replace an existing scene. On `409`,
   re-fetch the scene and reconcile. Omitting the parameter preserves the legacy
   unguarded last-write-wins behavior. A `409` writes no scene revision, creates
   no asset and does not increment `reuse_count`; a missing stored asset may still
   be repaired as a side effect.
3. **Partial updates.** `/api/v1/scenes/{scene_id}/update` applies only the node
   fields that are present; omitted fields are unchanged. `metadata` is replaced
   wholesale (not deep-merged). There is no edge update operation — use
   `edges_to_remove` plus `edges_to_add`. Send `expected_revision`; on `409`
   re-fetch the scene and reconcile instead of retrying blindly. Even when
   `expected_revision` is omitted, the write is compared with the revision the
   patch was built from, so a concurrent change also returns `409`.
4. **Revisions.** An identical instruction creates no new revision. A
   metadata/group-only change creates a new revision while reusing the same asset
   and returning `cache_hit: true`. Always store the returned `revision`; never
   treat `cache_hit` or a stable `asset_id` as proof that the revision did not
   change. A guarded `/visualize` with a stale revision returns `409` even when
   the submitted instruction is byte-identical.
5. **Polling only.** Milestone 1 has no WebSocket or SSE transport, and
   `GET /scenes/{scene_id}` has no ETag/conditional-GET support. Poll every
   3–5 seconds only while a scene is open, reuse the response from your own
   updates instead of immediately issuing another GET, stop polling when the
   scene closes, and back off on errors.
6. **Reconnects.** `GET /api/v1/scenes/{scene_id}` restores the latest state and
   the complete revision history.
7. **Remote deployments.** Add Engine 2's origin to
   `MIND_BRIDGE_CORS_ORIGINS`, keep the prefix aligned with `VITE_API_PREFIX`,
   and send/report `X-Request-ID` for correlation. No authentication is
   implemented yet — deploy on a trusted network only.

The React workspace in `frontend/` is the reference consumer and doubles as a
test harness for the contract.

---

## What is implemented

- JSON validation with meaningful 422 errors, strict `schema_version` policy,
  layered auto-layout (LR/TB), cycle warning.
- Input limits: 1 MiB request bodies (`413`), metadata depth 5 / 4 KiB (`422`),
  all env-configurable.
- Deterministic, self-contained SVG: icons by node kind, arrowheads, curved edges,
  edge-label pills, header/footer, responsive `viewBox`.
- Display hardening: grapheme-aware width/wrap/truncation for Latin, CJK and emoji,
  title/description/node-label overflow warnings, edge-label collision avoidance,
  and perpendicular stacking for parallel-edge labels.
- Structured JSON logs with `X-Request-ID`; scene operations and API errors are
  logged without user content.
- Visual Asset Memory: filesystem + SQLite, unique asset ids, exact-match reuse
  with `reuse_count`, categories/tags columns, listing/filtering.
- Scene revisions: create, incremental update, history, retrieval.
- Opt-in AI image generation: `render_mode: "image"` routes through the
  provider-independent adapter layer, fingerprints provider/model/size/quality/
  prompt, reuses exact image assets, never regenerates missing files, maps
  provider failures to safe HTTP statuses, and coalesces identical requests
  in-process. OpenAI + Gemini adapters use injectable transports, mocked tests
  and no automatic retries.
- React workspace: JSON editor, SVG / AI image mode control, rendered SVG and
  image canvas (zoom/fit/download), scene update demo, asset memory panel,
  current-image preview, health status.
- Backend and frontend tests; Ruff/mypy/ESLint configuration; CI workflow;
  production build.

## What is not implemented yet

- Cross-process single-flight for image generation. The in-process lock prevents
  duplicate paid calls within one worker; multiple workers/hosts can still issue
  duplicates, though asset adoption keeps storage correct.
- A frontend capability probe for image providers. The workspace explains the
  requirement up front and surfaces the backend's safe `503` message when image
  generation is unavailable; it does not call a provider to test credentials.
- Similarity search over assets (exact-match only; `content_hash` is the future seam).
- Real-time push delivery (websockets/SSE); consumers request/refresh.
- Auth, multi-user isolation, deployment packaging.
- Non-diagram scene types.

---

## Notes

- **Windows Smart App Control:** `pydantic>=2.12` wheels ship an unsigned
  `pydantic_core` DLL that this machine's application-control policy blocks. The
  backend therefore pins `pydantic>=2.9,<2.12` (2.11.9 verified working). No
  security settings were changed.
- **Starlette 1.x:** FastAPI 0.165 requires Starlette ≥1.6 (verified with 1.10).
  Its TestClient prefers `httpx2`; `requirements-dev.txt` installs it, so the test
  suite runs without the httpx deprecation warning.
- Generated assets and the SQLite file are runtime data and are git-ignored.
