# Engine 2 → Engine 3 Integration Handoff

Engine 3 renders structured `VisualInstruction` payloads into deterministic SVG,
stores them in Visual Asset Memory, and tracks scene revisions. Engine 2 owns
intent and structure; Engine 3 owns rendering and state.

**Status:** contract frozen for milestone 1. The four integration decisions are
finalized: explicit schema version, partial node patches, a revision for
metadata-only edits, and polling-only delivery.

All examples below were checked against `backend/app/schemas/scene.py` and the
live routes and captured from a local test run on 2026-11-19. Asset ids, hashes
and timestamps are run-specific. The inline `svg` is abbreviated in the examples.

---

## 1. Connection

| Item | Value |
| ---- | ----- |
| Base URL | `http://localhost:8000/api/v1` (default) |
| Path prefix | Configurable with `MIND_BRIDGE_API_PREFIX`; discover it at `GET /` |
| Discovery | `GET /` returns `api_prefix`, `schema_version`, `schema_versions`, `max_request_bytes` |
| Content type | `application/json` on every request |
| OpenAPI | `http://localhost:8000/docs` |
| Request id | Every response carries `X-Request-ID`; a well-formed inbound value (`A-Z a-z 0-9 . _ -`, 1–128 chars) is preserved, otherwise one is generated |

| Method | Path | Purpose |
| ------ | ---- | ------- |
| `POST` | `/visualize` | Render a full instruction; optional `?expected_revision=` guard |
| `POST` | `/scenes/{scene_id}/update` | Apply an incremental patch |
| `GET`  | `/scenes/{scene_id}` | Latest state, full revision history, current SVG |
| `GET`  | `/assets` | List Visual Asset Memory (`?kind=&q=&limit=&offset=`) |
| `GET`  | `/assets/{asset_id}` | Asset metadata |
| `GET`  | `/assets/{asset_id}/content` | Raw asset bytes (immutable cache headers) |
| `GET`  | `/assets/{asset_id}/thumbnail` | Thumbnail bytes |
| `GET`  | `/samples/visual-instruction` | Bundled demo instruction |
| `GET`  | `/health` | Readiness probe + `schema_version`/`schema_versions` |

Asset `url` and `thumbnail_url` are **prefix-relative** (for example
`/api/v1/assets/ast_…/content`). Resolve them against the Engine 3 origin, not the
page origin.

---

## 2. Contract version and limits

**Engine 2 must always send `"schema_version": "1.0"` explicitly.** Engine 3
defaults an omitted field to `"1.0"` for hand-written payloads, but that is a
convenience, not part of the integration contract. Any other version returns
`422` and lists the supported versions.

| Limit | Value | Failure |
| ----- | ----- | ------- |
| Supported schema | `"1.0"` only | `422` |
| Whole request body | 1 MiB (1 048 576 bytes), `MIND_BRIDGE_MAX_REQUEST_BYTES` | `413` |
| `metadata` nesting depth | 5 levels, `MIND_BRIDGE_METADATA_MAX_DEPTH` | `422` |
| `metadata` serialized size | 4 KiB per node, `MIND_BRIDGE_METADATA_MAX_BYTES` | `422` |
| Nodes | 1–80 | `422` |
| Edges | ≤ 160 | `422` |
| Title / description | 120 / 400 chars | `422` |
| Node label / edge label | 80 / 48 chars | `422` |
| Per-update node operations | 40 each for add/update/remove | `422` |
| Per-update edge operations | 80 each for add/remove | `422` |
| Id pattern | `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$` | `422` |

The top-level `metadata` object counts as nesting level 1. `metadata` must be
JSON-serializable.

---

## 3. Initial visualization request

For an intentional initial creation, send `expected_revision=0`:

`POST /api/v1/visualize?expected_revision=0` with the bundled demo payload
(`sample_data/bus_tracking.json`):

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

Other contract rules:

- `type` is `"diagram"`.
- `kind` ∈ `bus | server | passenger | phone | database | cloud | service | sensor | generic`.
- `direction` ∈ `LR | TB`; `style` ∈ `solid | dashed`.
- `position` is optional per node; auto-layout is skipped only when every node has one.
- `metadata` and `group` are never rendered, and `metadata` is excluded from the
  asset fingerprint.
- An edge `id` may be omitted (defaults to `<source>-><target>`); give parallel
  edges explicit ids.

`expected_revision` is an optional **query** parameter on `/visualize`:

- `0` is create-only. An absent scene is created at revision 1; an existing
  scene returns `409`.
- A positive value must match the current revision. A mismatch returns `409`.
- Omitted preserves the legacy last-write-wins behavior; use the guard when a
  concurrent update or another visualization must not be silently overwritten.
- Do not put `expected_revision` in the JSON body. `VisualInstruction` forbids
  unknown fields, so a body field returns `422`.
- On `409`, re-fetch the scene and reconcile; do not blindly retry the same
  revision. A create request that succeeded but lost its response will return
  `409` on retry because the scene now exists.

---

## 4. Successful visualization response

`HTTP 200` from the request above:

```json
{
  "scene_id": "bus-tracking-demo",
  "schema_version": "1.0",
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

- `svg` is complete, self-contained and XML-escaped; inject it into a sandboxed
  host element. Element ids are namespaced `mb-<scene>-<hash>-…`.
- `asset` is the complete `AssetSummary`. `reuse_count` rises on exact-match reuse
  of a successful write.
- `warnings` is a human-readable `string[]`; treat it as opaque and display it.
  It is also returned on cache hits and scene reads.
- `cache_hit: true` means no new asset was written. It does **not** mean the
  revision was unchanged (see section 7).

---

## 5. Partial node update

`POST /api/v1/scenes/bus-tracking-demo/update`:

```json
{
  "expected_revision": 1,
  "nodes_to_update": [
    { "id": "bus-1", "label": "Bus 101 (Delayed)" }
  ]
}
```

Response `HTTP 200`: same shape as section 4, with `"revision": 2`,
`"cache_hit": false`, and a new `asset.asset_id` (`ast_756837684ff34414` in the
captured run). Only `bus-1.label` changed.

### Field rules

| Field in a `nodes_to_update` entry | Behaviour |
| ---------------------------------- | --------- |
| `id` | Required; must match an existing node in the merged scene |
| `label`, `kind` | Applied when non-null; `null` is ignored and cannot clear them |
| `group` | Applied as supplied; `null` clears it |
| `position` | Applied as supplied; `null` clears it |
| `metadata` | **Replaced wholesale**, never deep-merged; `{}` clears it, `null` is ignored |

- Only fields present in the JSON are applied; omitted fields are unchanged.
- At the top level, `title` and `options` are applied only when non-null;
  `description` is set from the supplied value and `null` clears it.
- Updating an id that is not in the scene (including one removed in the same
  patch) returns `422`.
- There is **no edge update operation**. Replace an edge with `edges_to_remove`
  plus `edges_to_add` in the same patch.
- Operation order inside one request: edge removals → node removals (connected
  edges are dropped) → node updates → node additions → edge additions.
- `expected_revision` is optional but recommended. If it does not match the
  current revision, the update fails with `409` and no scene revision is written.
  On `409`, re-fetch the scene and reconcile; do not blindly retry the same
  revision.

---

## 6. Metadata-only update

```json
{
  "expected_revision": 2,
  "nodes_to_update": [
    {
      "id": "gps-server",
      "metadata": { "role": "hub", "region": "north" }
    }
  ]
}
```

Response `HTTP 200` (trimmed to the changed facts):

```json
{
  "scene_id": "bus-tracking-demo",
  "schema_version": "1.0",
  "revision": 3,
  "cache_hit": true,
  "asset": {
    "asset_id": "ast_756837684ff34414",
    "reuse_count": 1,
    "url": "/api/v1/assets/ast_756837684ff34414/content"
  },
  "warnings": []
}
```

The whole metadata object was replaced: the previous
`{"role": "central-hub", "protocol": "HTTPS"}` is gone, and `protocol` is not
retained. The revision advanced because the scene instruction changed, while the
same asset was reused because metadata is not part of the visual fingerprint.

---

## 7. Revision semantics

| Situation | Revision | Asset | `cache_hit` |
| --------- | -------- | ----- | ----------- |
| Byte-identical instruction | unchanged | same `asset_id` | `true` |
| `metadata`/`group`-only change | **+1** | same `asset_id` | `true` |
| Rendered field changed | +1 | new or reused asset | `true`/`false` |

- Revisions are per scene, monotonic and immutable; `GET /scenes/{id}` returns
  the complete history newest-first (for example `[3, 2, 1]`).
- **Always store the `revision` returned by the last successful call.**
- **Never** interpret `cache_hit: true` or a stable `asset_id` as proof that the
  revision did not change. Metadata-only edits are the normal case where both are
  stable but the revision advances.
- Repeating a byte-identical request returns the current revision and reuses the
  asset without inserting history.
- A guarded `/visualize` with a stale revision returns `409` even when its
  instruction is byte-identical to the current scene.

---

## 8. Error responses

### 409 — stale or mismatched `expected_revision`

```json
{
  "detail": "Scene 'bus-tracking-demo' is at revision 3, but revision 2 was expected. Re-fetch the scene and retry."
}
```

For `/visualize`, `expected_revision=0` on an existing scene and a positive
value on an absent scene also return `409`. No scene revision is written, no new
asset is created and `reuse_count` is not incremented. A missing stored asset may
still be repaired (file restored) as a side effect, but its reuse count is
unchanged.

Re-fetch `GET /scenes/{scene_id}`, apply your delta to the fresh state, and retry
with the new revision. For a create request whose response was lost, re-fetch
the scene and reconcile before retrying.

### 413 — request body over the limit

Captured with a 512-byte test configuration:

```json
{
  "detail": "Request body exceeds the 512-byte limit."
}
```

The message uses the configured limit; the default deployment value is
`1048576` (1 MiB). The check covers both declared `Content-Length` and chunked
bodies.

### 422 — unsupported schema version

```json
{
  "detail": [
    {
      "type": "value_error",
      "loc": ["body", "schema_version"],
      "msg": "Value error, Unsupported schema_version '2.0'. Supported versions: 1.0.",
      "input": "2.0"
    }
  ]
}
```

### 422 — metadata limits

```json
{
  "detail": [
    {
      "type": "value_error",
      "loc": ["body", "nodes", 3, "metadata"],
      "msg": "Value error, metadata must be at most 5 levels deep (got 8)"
    }
  ]
}
```

`loc` identifies the offending node index. The same envelope covers every field
validation failure, including oversize serialized metadata and per-request
operation caps.

### Other statuses

| Status | Meaning |
| ------ | ------- |
| `404` | Unknown scene or asset id |
| `422` | Invalid merged scene, unknown node/edge id, duplicate edge id, graph rule violation |
| `503` | Asset storage unavailable or a missing asset cannot be regenerated |

`detail` is a string for `404`/`409`/`503` and an array for `422`. Handle both.

---

## 9. Polling policy (milestone 1)

Milestone 1 is **poll-only**:

- No WebSocket or SSE transport is available.
- `GET /scenes/{scene_id}` has no `ETag`/`If-None-Match` support and always
  returns the complete scene plus the full revision history, so payload size grows
  with edit history.

Recommended Engine 2 policy:

1. Poll every **3–5 seconds** only while a scene is open.
2. Reuse the response from your own successful update instead of immediately
   issuing another `GET`.
3. Treat a change in `current_revision` as the refresh trigger.
4. Stop polling when the scene closes or loses focus.
5. Back off on errors (for example exponential backoff with jitter) and resume at
   the normal interval after a success.
6. Use `GET /scenes/{id}` on reconnect to restore state; do not re-visualize.

---

## 10. Assets, warnings and observability

- Assets are content-addressed (`ast_<16 hex>`); exact matches are reused and
  `reuse_count` increments on successful writes. Asset content URLs are immutable
  (`Cache-Control: public, max-age=31536000, immutable`); thumbnails for SVG
  assets reuse the vector.
- The SVG is namespaced per scene (`mb-<scene>-<hash>-…`), so multiple diagrams
  can share one page.
- `warnings` reports truncation, label overlap or canvas placement and graph
  cycles. Warnings never change the HTTP status and must not be parsed for
  control flow.
- Logs are JSON on stdout. Scene events carry `scene_id`, `revision`,
  `cache_hit` and `warning_count`; request bodies, titles, labels and metadata are
  never logged. Include `X-Request-ID` when reporting failures.

---

## 11. Integration checklist

1. Discover `api_prefix` and `schema_version` from `GET /`; refuse to run against
   an unsupported schema.
2. Always send `"schema_version": "1.0"`.
3. Enforce the body, node, edge and metadata limits client-side before sending.
4. Persist `scene_id` and `revision`; send `expected_revision` on updates and
   guarded visualizations. Use `0` for create-only visualizations.
5. On `409`, re-fetch and reconcile; never blindly retry.
6. Store the returned revision on every success, including metadata-only edits.
7. Resolve asset URLs against the Engine 3 origin; prefer inline `svg` for
   immediate rendering.
8. Poll at 3–5 seconds only while a scene is open, and back off on errors.
9. Do not expose Engine 3 publicly: milestone 1 has no authentication,
   rate limiting or multi-user isolation.

Not implemented in milestone 1: WebSocket/SSE push, conditional GET, AI image
generation, similarity search, and deployment packaging.
