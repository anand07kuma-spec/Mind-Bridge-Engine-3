# Engine 2 → Engine 3 Integration Contract

**Status:** Phase 4, Milestone 3 handoff. Written against the implemented Engine 3
code (`backend/app/schemas/scene.py`, `backend/app/api/routes.py`) and verified by
the Engine 3 test suite. It supersedes nothing in `docs/ENGINE2_HANDOFF.md`; that
document covers the SVG-only milestone. This document covers **SVG and explicit
AI image mode**.

- Base URL (default): `http://localhost:8000`
- API prefix (default): `/api/v1`
- OpenAPI UI: `GET /docs`
- Discovery: `GET /` returns `api_prefix`, `schema_version`, `schema_versions`,
  `max_request_bytes`, `health` and `visualize`.

All asset ids, hashes, dimensions and timestamps below are run-specific. Treat
them as opaque strings; never parse them.

---

## A. Engine 3 purpose and responsibility

Engine 3 is a **standalone visualization service**. Engine 2 decides *what* to
show and sends a validated `VisualInstruction`. Engine 3:

1. validates the instruction and its graph rules;
2. renders a deterministic, self-contained SVG **or** requests one AI image when
   `render_mode` is explicitly `"image"`;
3. stores the result in Visual Asset Memory (content-addressed assets + SQLite
   scene/revision metadata);
4. returns the scene revision, asset metadata and (for SVG) inline markup;
5. serves immutable asset bytes and scene state on later reads.

Engine 3 does **not** interpret speech, intent, or free-form text. It never
generates an image implicitly: image mode is opt-in per request and per update.

**Responsibility boundary**

| Engine 2 owns | Engine 3 owns |
| --- | --- |
| Intent, scene structure, labels, graph, `scene_id`, when to update | Validation, layout, SVG rendering, optional image generation, asset storage, revisions |
| Choosing `render_mode` | Honouring it, enforcing provider configuration and safe errors |
| Persisting the last known `revision` | Monotonic per-scene revision history |

---

## B. Exact request JSON — SVG mode

`POST /api/v1/visualize?expected_revision=0`

`expected_revision` is a **query parameter**. Do **not** put it in the JSON body:
`VisualInstruction` forbids unknown fields and returns `422`.

```json
{
  "schema_version": "1.0",
  "scene_id": "bus-tracking-demo",
  "type": "diagram",
  "title": "Live Bus Tracking System",
  "description": "Three buses stream GPS coordinates to the central server, which pushes live locations to the passenger app.",
  "options": {
    "direction": "LR",
    "show_edge_labels": true
  },
  "nodes": [
    { "id": "bus-1", "label": "Bus 101", "kind": "bus", "group": "fleet" },
    { "id": "bus-2", "label": "Bus 102", "kind": "bus", "group": "fleet" },
    { "id": "bus-3", "label": "Bus 103", "kind": "bus", "group": "fleet" },
    {
      "id": "gps-server",
      "label": "GPS Server",
      "kind": "server",
      "metadata": { "role": "central-hub", "protocol": "HTTPS" }
    },
    { "id": "passenger", "label": "Passenger App", "kind": "passenger" }
  ],
  "edges": [
    { "id": "bus1-gps", "source": "bus-1", "target": "gps-server", "label": "GPS ping" },
    { "id": "bus2-gps", "source": "bus-2", "target": "gps-server", "label": "GPS ping" },
    { "id": "bus3-gps", "source": "bus-3", "target": "gps-server", "label": "GPS ping" },
    { "id": "gps-passenger", "source": "gps-server", "target": "passenger", "label": "Live location" }
  ]
}
```

- `render_mode` may be omitted; it defaults to `"svg"`. Sending
  `"render_mode": "svg"` explicitly is equivalent and recommended for clarity.
- `type` must be `"diagram"`.
- `kind` ∈ `bus | server | passenger | phone | database | cloud | service | sensor | generic`.
- `direction` ∈ `LR | TB`; edge `style` ∈ `solid | dashed`.
- Edge `id` may be omitted; it defaults to `<source>-><target>`. Parallel edges
  need explicit ids.
- `metadata` and `group` are never rendered.

### Input limits

| Item | Limit | Failure |
| --- | --- | --- |
| Request body | 1 MiB (`MIND_BRIDGE_MAX_REQUEST_BYTES`) | `413` |
| `schema_version` | `"1.0"` only | `422` |
| Nodes | 1–80 | `422` |
| Edges | ≤ 160 | `422` |
| Title / description | 120 / 400 chars | `422` |
| Node label / edge label | 80 / 48 chars | `422` |
| `metadata` depth / serialized size | 5 levels / 4 KiB per node | `422` |
| Ids | `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$` | `422` |

---

## C. Exact request JSON — AI image mode

`POST /api/v1/visualize?expected_revision=0`

Same payload as section B, plus exactly one field:

```json
{
  "schema_version": "1.0",
  "scene_id": "bus-tracking-image",
  "type": "diagram",
  "render_mode": "image",
  "title": "Live Bus Tracking System",
  "description": "Three buses stream GPS coordinates to the central server.",
  "options": { "direction": "LR", "show_edge_labels": true },
  "nodes": [
    { "id": "bus-1", "label": "Bus 101", "kind": "bus" },
    { "id": "gps-server", "label": "GPS Server", "kind": "server" },
    { "id": "passenger", "label": "Passenger App", "kind": "passenger" }
  ],
  "edges": [
    { "id": "bus1-gps", "source": "bus-1", "target": "gps-server", "label": "GPS ping" },
    { "id": "gps-passenger", "source": "gps-server", "target": "passenger", "label": "Live location" }
  ]
}
```

- `"render_mode": "image"` is the **only** way to request an image. Provider
  configuration or API-key presence never triggers image generation.
- Image mode requires a backend provider (`MIND_BRIDGE_IMAGE_PROVIDER=openai` or
  `gemini` plus the matching backend key). Without it the request returns `503`
  and SVG continues to work.
- Do not send model names, sizes, prompts or API keys. Engine 3 builds the prompt
  deterministically from the structured scene and uses backend configuration.

---

## D. Exact response JSON — both modes

Both modes return the same `VisualizeResponse` envelope. `HTTP 200`.

### D.1 SVG mode

```json
{
  "scene_id": "bus-tracking-demo",
  "schema_version": "1.0",
  "render_mode": "svg",
  "revision": 1,
  "cache_hit": false,
  "asset": {
    "asset_id": "ast_3b195e01c0de4c86",
    "kind": "svg",
    "content_hash": "e1c1bbe959e00fbadafd6200a416813c8c37181776bfeb271c0b05da70415109",
    "mime_type": "image/svg+xml",
    "width": 920,
    "height": 516,
    "category": "diagram",
    "tags": ["bus", "diagram", "passenger", "server", "svg"],
    "provider": "engine3-svg",
    "reuse_count": 0,
    "url": "/api/v1/assets/ast_3b195e01c0de4c86/content",
    "thumbnail_url": "/api/v1/assets/ast_3b195e01c0de4c86/thumbnail",
    "created_at": "2026-11-22T05:55:03Z",
    "last_used_at": "2026-11-22T05:55:03Z"
  },
  "svg": "<svg xmlns=\"http://www.w3.org/2000/svg\" …>…</svg>",
  "warnings": []
}
```

### D.2 Image mode

```json
{
  "scene_id": "bus-tracking-image",
  "schema_version": "1.0",
  "render_mode": "image",
  "revision": 1,
  "cache_hit": false,
  "asset": {
    "asset_id": "ast_9f2c71a4be0d5511",
    "kind": "image",
    "content_hash": "4d0f9a2f0d1c8e77a2f0c3b6d8e1a5f7c9b2d4e6f8a0c1b3d5e7f9a1c3b5d7e9",
    "mime_type": "image/png",
    "width": 1024,
    "height": 1024,
    "category": "diagram",
    "tags": ["bus", "diagram", "image", "passenger", "server"],
    "provider": "openai",
    "reuse_count": 0,
    "url": "/api/v1/assets/ast_9f2c71a4be0d5511/content",
    "thumbnail_url": "/api/v1/assets/ast_9f2c71a4be0d5511/thumbnail",
    "created_at": "2026-11-22T06:00:00Z",
    "last_used_at": "2026-11-22T06:00:00Z"
  },
  "svg": null,
  "warnings": []
}
```

### D.3 Field contract

| Field | Meaning |
| --- | --- |
| `scene_id` | Echo of the instruction. |
| `schema_version` | `"1.0"`. |
| `render_mode` | `"svg"` or `"image"`; the mode actually stored for this revision. |
| `revision` | Monotonic per-scene revision. **Persist this value.** |
| `cache_hit` | `true` when an identical asset was reused and no new asset bytes were written. It does **not** mean the revision is unchanged. |
| `asset` | Complete `AssetSummary` for the current revision. |
| `asset.kind` | `"svg"` or `"image"`. |
| `asset.mime_type` | `image/svg+xml` for SVG; `image/png`, `image/jpeg` or `image/webp` for images. |
| `asset.url` | Prefix-relative content URL. Resolve against the Engine 3 origin. |
| `asset.thumbnail_url` | Prefix-relative thumbnail URL. For images this may be the full image (no image resizer). |
| `svg` | Full inline SVG for SVG mode; **always `null` for image mode**. |
| `warnings` | Opaque `string[]`; display it, never branch on its text. |

SVG markup is XML-escaped server-side and namespaced per scene
(`mb-<scene>-<hash>-…`). Inject it into a sandboxed host element. Raster images
must be displayed through `<img src="{asset.url}">`, never inlined as HTML.

---

## E. Scene update contract

`POST /api/v1/scenes/{scene_id}/update`

Request model `SceneUpdate` (unknown fields are rejected):

```json
{
  "expected_revision": 1,
  "title": "Live Bus Tracking System",
  "description": "Updated description.",
  "options": { "direction": "TB", "show_edge_labels": true },
  "render_mode": "svg",
  "nodes_to_add": [
    { "id": "eta-service", "label": "ETA Service", "kind": "service" }
  ],
  "nodes_to_update": [
    { "id": "bus-1", "label": "Bus 101 (Delayed)" }
  ],
  "nodes_to_remove": ["bus-3"],
  "edges_to_add": [
    { "id": "gps-eta", "source": "gps-server", "target": "eta-service", "label": "ETAs" }
  ],
  "edges_to_remove": ["bus3-gps"]
}
```

All fields are optional. The response is the same `VisualizeResponse` shape as
section D.

### E.1 `expected_revision`

- Optional integer `>= 0`. Recommended on every update.
- If present and not equal to the scene's current revision → `409`, no revision
  is written, no provider is called, and the current asset is unchanged.
- If omitted, Engine 3 still persists against the revision it read, so a
  concurrent update cannot silently overwrite it (it fails with `409` instead).
- On `409`: re-fetch the scene, re-apply your delta to the fresh state, and retry
  with the new revision. Never blindly retry the same revision.

### E.2 Partial node updates

`nodes_to_update` entries use `NodePatch`:

| Field | Behaviour |
| --- | --- |
| `id` | Required; must match an existing node in the merged scene. |
| `label`, `kind` | Applied when non-null; `null` is ignored. |
| `group` | Applied as supplied; `null` clears it. |
| `position` | Applied as supplied; `null` clears it. |
| `metadata` | **Replaced wholesale**, never deep-merged; `{}` clears it, `null` is ignored. |

- Only fields present in the JSON are applied.
- At the top level, `title` and `options` apply when non-null; `description` is
  set from the supplied value and `null` clears it.
- There is no edge-update operation. Remove and re-add the edge in one patch.
- Operation order: edge removals → node removals (connected edges drop) → node
  updates → node additions → edge additions.
- Updating an unknown id, duplicate ids, dangling edges or graph violations
  return `422`.
- Per-update caps: 40 node add/update/remove operations each, 80 edge
  add/remove operations each.

### E.3 `render_mode` preservation and switching

- **Omitted** on update → the scene keeps its stored mode.
- `"render_mode": "svg"` → the resulting revision is SVG; `svg` is returned and
  no provider is called.
- `"render_mode": "image"` → the resulting revision is an image; this is an
  explicit, possibly paid generation.
- Switching from image to SVG does not delete the old image asset.
- Switching from SVG to image with no provider configured returns `503` and
  leaves the scene at its previous revision.

### E.4 Revision behaviour

| Situation | Revision | Asset | `cache_hit` |
| --- | --- | --- | --- |
| Byte-identical instruction | unchanged | same `asset_id` | `true` |
| Metadata/group-only change | +1 | may reuse the same visual asset | `true` |
| Rendered field changed | +1 | new or reused asset | `true`/`false` |
| Mode switch | +1 | asset of the new kind | `false` normally |
| Stale `expected_revision` | unchanged (`409`) | unchanged | n/a |

Revisions are per scene, monotonic and immutable. `GET /scenes/{id}` returns the
full history newest-first. Always store the `revision` from the last successful
call, including metadata-only edits.

---

## F. Asset retrieval

Given an `asset` object:

| Purpose | Request |
| --- | --- |
| Raw bytes | `GET {asset.url}` (for example `/api/v1/assets/ast_…/content`) |
| Thumbnail | `GET {asset.thumbnail_url}` (for example `/api/v1/assets/ast_…/thumbnail`) |
| Metadata | `GET /api/v1/assets/{asset_id}` |
| List | `GET /api/v1/assets?kind=image&q=bus&limit=50&offset=0` |

- `asset_id` format: `ast_<16 hex>`. Opaque; use it only in URLs.
- MIME types: SVG `image/svg+xml`; images `image/png`, `image/jpeg` or
  `image/webp` (validated by signature and MIME agreement).
- Content URLs are immutable and served with
  `Cache-Control: public, max-age=31536000, immutable`. Cache them freely.
- `url` and `thumbnail_url` are **prefix-relative**. Resolve them against the
  Engine 3 origin, not the Engine 2 or browser page origin.
- Unknown asset → `404`. A known asset whose file is missing → `503`; images are
  never regenerated automatically.
- For SVG assets, the thumbnail is the same vector document. For image assets,
  the thumbnail may be the full image; do not assume it is smaller.

---

## G. Error status codes

Error body is FastAPI's standard `{"detail": …}`. `detail` is a **string** for
`404`/`409`/`429`/`502`/`503`/`504` and an **array** for `422`. Handle both.

| Status | When | Engine 2 action |
| --- | --- | --- |
| `422` | Validation failure, unsupported `schema_version`, graph violation, unknown/duplicate ids in an update, or provider content-policy rejection | Fix the payload (or the prompt content); do not retry unchanged. |
| `429` | Image provider rate limit. Response carries `Retry-After` seconds when known. | Wait at least `Retry-After`, then retry with backoff. |
| `502` | Image provider authentication failure or malformed/upstream response. | Do not retry automatically; report to the Engine 3 operator. |
| `503` | No image provider configured, provider unavailable, asset storage/recovery failure, or a missing image file that must not be regenerated. | For image mode, fall back to SVG or surface the message. For storage errors, retry with backoff. |
| `504` | Image provider timeout. | Retry with backoff or fall back to SVG. |
| `409` | Stale/mismatched `expected_revision`, or a concurrent scene operation. | Re-fetch the scene, reconcile, retry with the new revision. |
| `404` | Unknown scene or asset id. | Treat as deleted/absent; re-create deliberately. |
| `413` | Request body over 1 MiB. | Reduce the payload; do not retry unchanged. |
| `500` | Server configuration or asset-integrity error. | Report to the Engine 3 operator. |

Provider messages are constructed to be safe: they never include API keys,
prompts or raw provider bodies. Display the `detail` as-is.

---

## H. Polling behaviour

Engine 3 is **poll-only**. There is no WebSocket or SSE transport, and
`GET /scenes/{id}` has no `ETag`/`If-None-Match` support. It returns the full
scene plus full revision history, so payload size grows with edit history.

Recommended Engine 2 policy:

1. Poll `GET /api/v1/scenes/{scene_id}` every **3–5 seconds only while the scene
   is open/visible**.
2. **Stop polling** when the scene closes, is hidden, or loses focus.
3. After your own successful `/visualize` or `/update`, use that response
   directly; do not issue an immediate redundant `GET`.
4. Treat a change in `current_revision` as the refresh trigger.
5. On reconnect, restore with `GET /scenes/{id}`; do not re-visualize.
6. Back off exponentially with jitter on errors; resume the normal interval
   after a success.

---

## I. Provider configuration

Provider selection and credentials are **backend-only**.

| Setting | Values / default |
| --- | --- |
| `MIND_BRIDGE_IMAGE_PROVIDER` | `none` (default), `openai`, `gemini` |
| `OPENAI_API_KEY` | Required when provider is `openai`; never sent by clients |
| `GEMINI_API_KEY` | Required when provider is `gemini`; never sent by clients |
| `MIND_BRIDGE_IMAGE_MODEL` | `gpt-image-1` / `gemini-2.5-flash-image` by default |
| `MIND_BRIDGE_IMAGE_SIZE` | `1024x1024` |
| `MIND_BRIDGE_IMAGE_QUALITY` | `standard` |
| `MIND_BRIDGE_IMAGE_TIMEOUT_SECONDS` | `60` |
| `MIND_BRIDGE_IMAGE_MAX_RETRIES` | `0` |
| `MIND_BRIDGE_IMAGE_MAX_BYTES` | `20971520` (20 MiB) |
| `MIND_BRIDGE_IMAGE_PROMPT_MAX_CHARS` | `4000` |

Rules:

- **Exactly one provider is active at a time.** The factory constructs only the
  provider named by `MIND_BRIDGE_IMAGE_PROVIDER`.
- Provider construction performs **no network I/O**. A missing key yields an
  unavailable provider; startup and SVG rendering still work.
- **SVG works with no image provider at all.**
- Image generation requires `"render_mode": "image"` on the request or update.
- **Engine 2 and the frontend must never send API keys**, model names, prompts or
  provider URLs. Engine 3 never returns them.
- There are no automatic provider retries by default. `429`/`504` handling is the
  caller's responsibility.

---

## J. Copy-paste-ready examples

Set these first:

```bash
export ENGINE3_URL="http://localhost:8000"
export ENGINE3_PREFIX="/api/v1"
```

### J.1 Health and discovery

```bash
curl -s "$ENGINE3_URL/" | jq
curl -s "$ENGINE3_URL$ENGINE3_PREFIX/health" | jq
```

### J.2 Create an SVG scene (guarded)

```bash
curl -s -X POST "$ENGINE3_URL$ENGINE3_PREFIX/visualize?expected_revision=0" \
  -H "Content-Type: application/json" \
  --data @sample_data/bus_tracking.json | jq
```

### J.3 Create an image scene (guarded)

```bash
curl -s -X POST "$ENGINE3_URL$ENGINE3_PREFIX/visualize?expected_revision=0" \
  -H "Content-Type: application/json" \
  --data '{
    "schema_version": "1.0",
    "scene_id": "bus-tracking-image",
    "type": "diagram",
    "render_mode": "image",
    "title": "Live Bus Tracking System",
    "description": "Three buses stream GPS coordinates to the central server.",
    "options": { "direction": "LR", "show_edge_labels": true },
    "nodes": [
      { "id": "bus-1", "label": "Bus 101", "kind": "bus" },
      { "id": "gps-server", "label": "GPS Server", "kind": "server" },
      { "id": "passenger", "label": "Passenger App", "kind": "passenger" }
    ],
    "edges": [
      { "id": "bus1-gps", "source": "bus-1", "target": "gps-server", "label": "GPS ping" },
      { "id": "gps-passenger", "source": "gps-server", "target": "passenger", "label": "Live location" }
    ]
  }' | jq
```

### J.4 Partial update

```bash
curl -s -X POST "$ENGINE3_URL$ENGINE3_PREFIX/scenes/bus-tracking-demo/update" \
  -H "Content-Type: application/json" \
  --data '{
    "expected_revision": 1,
    "nodes_to_update": [
      { "id": "bus-1", "label": "Bus 101 (Delayed)" }
    ]
  }' | jq
```

### J.5 Read the scene and download the asset

```bash
curl -s "$ENGINE3_URL$ENGINE3_PREFIX/scenes/bus-tracking-demo" | jq
curl -s -o scene.svg \
  "$ENGINE3_URL$ENGINE3_PREFIX/assets/ast_3b195e01c0de4c86/content"
```

### J.6 Python (httpx)

```python
from __future__ import annotations

import httpx

BASE = "http://localhost:8000/api/v1"


def create_scene(client: httpx.Client, instruction: dict) -> dict:
    """Create-only. Use expected_revision=0 so an existing scene is a 409."""
    response = client.post(
        f"{BASE}/visualize",
        params={"expected_revision": 0},
        json=instruction,
    )
    response.raise_for_status()
    return response.json()


def update_scene(client: httpx.Client, scene_id: str, revision: int, patch: dict) -> dict:
    response = client.post(
        f"{BASE}/scenes/{scene_id}/update",
        json={"expected_revision": revision, **patch},
    )
    if response.status_code == 409:
        # Re-fetch, reconcile your delta, then retry with the fresh revision.
        current = client.get(f"{BASE}/scenes/{scene_id}").json()
        raise RuntimeError(f"conflict: scene is at revision {current['current_revision']}")
    response.raise_for_status()
    return response.json()


def get_scene(client: httpx.Client, scene_id: str) -> dict:
    response = client.get(f"{BASE}/scenes/{scene_id}")
    response.raise_for_status()
    return response.json()


def download_asset(client: httpx.Client, asset: dict, path: str) -> None:
    # asset["url"] is prefix-relative; resolve against the Engine 3 origin.
    url = "http://localhost:8000" + asset["url"]
    response = client.get(url)
    response.raise_for_status()
    with open(path, "wb") as handle:
        handle.write(response.content)


if __name__ == "__main__":
    instruction = {
        "schema_version": "1.0",
        "scene_id": "bus-tracking-demo",
        "type": "diagram",
        "title": "Live Bus Tracking System",
        "options": {"direction": "LR", "show_edge_labels": True},
        "nodes": [
            {"id": "bus-1", "label": "Bus 101", "kind": "bus"},
            {"id": "gps-server", "label": "GPS Server", "kind": "server"},
            {"id": "passenger", "label": "Passenger App", "kind": "passenger"},
        ],
        "edges": [
            {"id": "bus1-gps", "source": "bus-1", "target": "gps-server", "label": "GPS ping"},
            {"id": "gps-passenger", "source": "gps-server", "target": "passenger", "label": "Live location"},
        ],
    }

    with httpx.Client(timeout=90.0) as client:
        scene = create_scene(client, instruction)
        print(scene["render_mode"], scene["revision"], scene["asset"]["kind"])

        # Switch to image mode explicitly; this may be a paid generation.
        image_scene = update_scene(
            client, scene["scene_id"], scene["revision"], {"render_mode": "image"}
        )
        print(image_scene["render_mode"], image_scene["asset"]["url"])

        if image_scene["asset"]["kind"] == "image":
            download_asset(client, image_scene["asset"], "scene.png")
```

---

## K. Limitations and integration assumptions

- **No authentication or multi-user isolation.** Deploy Engine 3 on a trusted
  network or behind an authenticating gateway. Do not expose it publicly.
- **Poll-only delivery.** No WebSocket/SSE push and no conditional GET.
- **Single-flight is in-process only.** One worker coalesces identical image
  requests; multiple workers/hosts can still issue duplicate paid calls. Storage
  remains correct because the first committed asset is adopted and losers' staged
  files are discarded.
- **Image fingerprints cover the full visual identity.** Changing a title, label,
  group or position can produce a new paid image even if the change looks minor.
  Batch visual edits where possible.
- **Missing image files are never regenerated automatically.** Reads return `503`
  or a scene warning. Re-request `render_mode: "image"` explicitly to regenerate.
- **No automatic provider retries** by default. Engine 2 must implement retry and
  fallback policy for `429`/`502`/`503`/`504`.
- **Image thumbnails may equal the full image** because the backend has no image
  resizer.
- **Schema `1.0` only.** Unsupported versions return `422`.
- **The scene detail payload grows with revision history.** Polling cost rises on
  long-lived, frequently edited scenes.
- **Cross-engine integration is pending.** Engine 1/Engine 2 source was not
  available in this workspace, so this contract was verified against Engine 3's
  own tests and a local reference consumer, not against a live Engine 2 build.
- **No real provider call has been verified.** OpenAI/Gemini behaviour is covered
  with injectable fake transports and deterministic fakes; a real paid call is an
  operator acceptance step.

### Assumptions Engine 2 must uphold

1. Send `"schema_version": "1.0"` on every request.
2. Persist `scene_id` and the returned `revision`; send `expected_revision` on
   updates and guarded visualizations (`0` = create-only).
3. Treat `409` as reconcile-and-retry, never as an idempotent success.
4. Resolve `asset.url`/`asset.thumbnail_url` against the Engine 3 origin.
5. Never send provider keys, prompts, model names or sizes.
6. Request images only when the product explicitly wants a generated image.
7. Stop polling when the scene is not visible.
