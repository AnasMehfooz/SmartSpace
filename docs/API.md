# SMART SPACE backend API: guide for the frontend

Base URL `http://127.0.0.1:<port>/api/v1`. Dev: `http://127.0.0.1:8765/api/v1`, token `dev`
(`scripts/dev_backend.ps1`, Swagger UI at `http://127.0.0.1:8765/docs`). In the app, Tauri's
`get_backend_info()` returns `{port, token}`.

- **Auth:** every request needs `Authorization: Bearer <token>`, except `GET /health`. Streams under `/…/events`
  also accept `?token=<token>` (for `EventSource`); prefer `fetch` streaming with the header.
- **CORS:** `tauri://localhost`, `http://tauri.localhost`, `https://tauri.localhost`, `http://localhost:5173`.
- **Errors (all 4xx/5xx):** `{"error": {"code": "FILE_NOT_FOUND", "message": "...", "details": {}}}`. Codes: SDD 10.10
  plus `MODEL_ACTIVE` (409, deleting the active model) and `NOT_FOUND`/`METHOD_NOT_ALLOWED`.
- **Pagination:** `?limit=50&offset=0` (max 200) → `{items, total, limit, offset}`.
- **Contract:** this file plus `docs/openapi.json` (also served live at `/openapi.json`, which needs the token unless `SS_DEV=1`, and
  browsable at `/docs` when `SS_DEV=1`). The frontend is JavaScript, so no type generation step is needed.
- Timestamps are ISO 8601 UTC (`2026-11-17T15:51:30Z`). IDs are integers, model IDs strings.

## Startup and onboarding flow

```
GET  /health                         poll until ready=true (no auth)
GET  /workspace                      null -> onboarding
GET  /models/catalog                 no active embedding model -> onboarding
1 Welcome
2 Pick folder (Tauri dialog) -> PUT /workspace {root_path}            400 WORKSPACE_PATH_INVALID shows the message
3 GET /system/hardware               {tier, cpu_name, ram_total_mb, gpus[...], best_gpu, battery, ...}
4 GET /models/recommendations        {tier, ctx_cap, recommended:{embedding,llm,ocr}, alternatives[], warnings[]}
  (let the user pick: recommended set, or any alternative; prefer ones that are installed or have local_copy)
  POST /models/{id}/download         202 {download_id}; progress on GET /models/downloads/events
                                     (a model with local_copy is installed from that copy, no internet)
  (first model of a role is auto-activated when the download completes)
5 POST /index/start                  202; progress on GET /index/events -> redirect to Home
```

## Endpoints

| Group | Method + path | Notes |
|---|---|---|
| system | `GET /health` | `{status, ready, version, db_ok, llm_state}` |
| | `POST /system/shutdown` | Tauri calls this on quit, waits 3 s, then kills |
| | `GET /system/info` | version, data_dir, sizes, python, platform, logs_dir |
| | `GET /system/hardware`, `POST /system/hardware/scan` | profile + `tier`; `best_gpu` = the usable GPU with the most VRAM (the one the tier and the LLM use; show this one, `gpus[]` keeps the driver's order) |
| models | `GET /models/catalog` | each: `status` (not_installed/downloading/installed/failed), `is_active`, `recommended`, `fits_hardware` (false → red "may be slow or crash"), `download` (live progress or queue position), `local_copy` (folder of an identical copy already on this PC, or null) |
| | `GET /models/recommendations` | `alternatives[]` lists **every** other embedding and language model, also ones above this PC's tier: `{id, role, display_name, size_bytes, fits_hardware, note, status, local_copy}`, installed and found-on-this-PC first. The recommendation is only a default; always let the user choose |
| | `POST /models/{id}/download` / `POST /models/{id}/cancel` | 507 `DISK_FULL` if not enough space |
| | `POST /models/{id}/activate` body `{confirm_reindex}` | embedding change with an existing index → 409 `REINDEX_REQUIRED` until `confirm_reindex: true`; 409 `INDEX_BUSY` while indexing |
| | `DELETE /models/{id}` | 409 `MODEL_ACTIVE` for the active one |
| | `GET /models/llm/status` | `{state: stopped/starting/ready/error, model_id, ctx, gpu_layers, backend, device, vram_used_mb, error}`; `device` is the GPU llama.cpp uses (e.g. `CUDA0`), null on CPU |
| | `POST /models/llm/start` (202, then poll status), `POST /models/llm/stop` | the LLM also starts lazily on first use |
| workspace | `GET /workspace` | `null` or `{root_path, status, created_at, last_scan_at, exists, counts}` |
| | `PUT /workspace` `{root_path, replace?}` | another path → 409 `WORKSPACE_EXISTS` unless `replace: true` (purges old index); response has `warnings[]` |
| | `DELETE /workspace` | unlink + delete the whole index (files untouched) |
| | `GET /workspace/stats` | by_status, by_ext, by_kind, chunks, summaries |
| | `GET/POST /workspace/exclusions`, `DELETE /workspace/exclusions/{id}` | glob relative to root (`Backups/**`, `*.tmp`); built-ins → 422 |
| files | `GET /files/tree?parent=Housing` | direct children: dirs (`child_count`, `file_count`, `indexed_count`) then files (`file_id`, `status`, `skip_reason`) |
| | `GET /files?folder&ext=pdf,docx&kind&status=error,skipped&tag&doc_type&q_name&sort=name\|mtime\|size\|indexed_at\|path&order` | paginated; each item also has `doc_type`, `tags` (list of names, `[]` if none) and `summary_short` (first ~160 chars of the summary, cut at a word, `…` if cut; `null` if no summary) |
| | `GET /files/{id}` | detail incl. `abs_path` (for `open_file`/`reveal_in_folder`), `summary{text,doc_type,model_id,generated_at}`, `tags[{name,source}]`, `chunk_count` |
| | `GET /files/{id}/chunks?limit=20` | extracted text (debug/preview) |
| | `GET /files/{id}/preview?page=1` | `image/jpeg` ≤ 1024 px, images and PDF pages |
| | `POST /files/{id}/reindex` | 202 |
| | `PUT /files/{id}/tags` `{tags: [...]}` | replaces **user** tags; AI tags stay |
| | `DELETE /files/{id}/index` | drops derived data; file becomes `skipped/excluded` |
| index | `POST /index/start` (202), `/pause`, `/resume`, `/retry-failed` | start needs a workspace + active embedding model |
| | `GET /index/status` | `{state: idle/scanning/indexing/paused, total, indexed, pending, processing, skipped, errors, summaries_done, summaries_pending, eta_s, paused_reason: user/battery/cpu, embedding_model}` |
| | `GET /index/errors?include_skipped=true` | paginated, error first |
| search | `POST /search` | below |
| chat | `GET/POST /chat/sessions`, `DELETE /chat/sessions` (all) | |
| | `GET/PATCH/DELETE /chat/sessions/{id}` | GET includes `messages[]` with `citations[]`, `generating` |
| | `POST /chat/sessions/{id}/messages` `{content, filters?}` | SSE stream, below |
| | `POST /chat/sessions/{id}/stop` | `{stopped}` |
| privacy | `GET /privacy/overview`, `GET /privacy/audit`, `GET /privacy/export` | |
| | `POST /privacy/purge` `{scope: index/file/chats/models/everything, file_ids?, confirm: true}` | 400 `CONFIRM_REQUIRED` without confirm → `{items_deleted, bytes_freed}` |
| settings | `GET /settings`, `PUT /settings` (partial) | unknown keys or wrong types → 422 |

File `status`: `pending | processing | indexed | skipped | error`; `skip_reason`: `unsupported | too_large | empty |
encrypted | no_text | excluded`; `summary_state`: `none | pending | done | failed`; `kind`: `document | image | other`.

## Search

```json
POST /search
{"query": "rental contract 2024", "mode": "full",
 "filters": {"ext": ["pdf"], "kind": null, "folder": "Housing", "tags": [], "doc_type": null,
             "date_from": "2024-01-01", "date_to": null},
 "limit": 10}
```
```json
{"query": "rental contract 2024", "mode": "full", "took_ms": 61,
 "results": [{"file_id": 15, "rel_path": "Housing/Mietvertrag_2024.pdf", "name": "Mietvertrag_2024.pdf",
   "ext": "pdf", "kind": "document", "score": 0.0328, "size_bytes": 2380, "mtime": "2024-03-02T10:11:00Z",
   "status": "indexed", "summary": "Rental agreement for …", "doc_type": "contract", "tags": ["miete", "kaution"],
   "snippet": {"chunk_id": 25, "page": 1, "source": "text", "text": "… monthly <mark>rent</mark> of …"}}]}
```
- `snippet.text` is **HTML-escaped**; only `<mark>` tags are added, so it is safe for `dangerouslySetInnerHTML`.
- `mode: "quick"` (Spotlight): max 6 results with `file_id, rel_path, name, ext, kind, score, snippet` only.
- `date_from/date_to` filter on file modification time (`YYYY-MM-DD` or ISO datetime). Queries are never stored.
- Only files Smart Space reads are returned: a file removed with `DELETE /files/{id}/index`, skipped or in error
  never shows up, not even when its name matches.

## Server-Sent Events

Read with `fetch` + a stream parser so the Bearer header can be sent (`event:` / `data:` lines, blank line ends an event).

**`GET /index/events`** - on connect you get the current `state_changed` and `progress`, then:

| event | data |
|---|---|
| `state_changed` | `{state, paused_reason}` |
| `progress` | `{indexed, total, eta_s, pending, processing, skipped, errors, summaries_done, summaries_pending}` (≤ 2/s) |
| `file_processing` | `{file_id, rel_path}` when a file starts extraction → "scanning now: …" (files skipped at scan time as `unsupported` never get it) |
| `file_indexed` / `file_skipped` / `file_removed` | `{file_id}` (+ `reason` for skipped) → invalidate `['files']` |
| `file_error` | `{file_id, message}` |
| `summary_done` / `summary_failed` | `{file_id}` |

**`GET /models/downloads/events`**: `queued {model_id, position}`, `progress {model_id, bytes_done, bytes_total,
speed_bps, eta_s, source}` (every 500 ms; `source: "local"` while a copy already on this PC is verified and installed), `completed {model_id}`, `failed {model_id, message}`, `cancelled {model_id}`.

**`POST /chat/sessions/{id}/messages`** (response is the stream, in this order):

| event | data |
|---|---|
| `meta` | `{message_id, user_message_id, model_id, search_only}`; `search_only: true` → show "Install a language model to get answers" |
| `files` | `{results: [...search result objects...]}` (top 5 file cards, always sent) |
| `token` | `{t: "The rent is "}` (many) |
| `sources` | `{citations: [{n, file_id, chunk_id, page, name, rel_path, score}]}` maps `[n]` in the answer. When the model forgets the `[n]` markers, the sources are still filled in: from a file name in the answer, or else from the numbers it quotes (`chunk_id` is then the passage holding them). So list `citations` as the sources even if the text has no `[n]`. An answer that says it could not find anything has **no** citations: show the `files` cards as "possibly related" instead |
| `done` | `{message_id, tokens_in, tokens_out, took_ms, stopped, failed}` |
| `error` | `{code: LLM_NOT_READY / LLM_START_FAILED / MODEL_NOT_INSTALLED / INTERNAL_ERROR, message, details}` (then `done`) |

Validation errors (unknown session 404, empty message 422, already generating 409) come back as normal JSON
before the stream starts. The first message also sets the session title (first 40 chars). The first answer after
start-up waits for llama-server to load (a few seconds on GPU).

```js
// minimal SSE-over-fetch reader
async function* sse(url, token, body) {
  const res = await fetch(url, { method: body ? "POST" : "GET", headers: { Authorization: `Bearer ${token}`,
    "Content-Type": "application/json" }, body: body ? JSON.stringify(body) : undefined });
  if (!res.ok) throw await res.json();               // {error:{code,message,details}}
  const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
  let buf = "", event = "message";
  for (;;) {
    const { value, done } = await reader.read(); if (done) return;
    buf += value;
    let i;
    while ((i = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, i).replace(/\r$/, ""); buf = buf.slice(i + 1);
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) yield { event, data: JSON.parse(line.slice(5)) };
    }
  }
}
```

## Settings (defaults)

`language "en"`, `quick_search_shortcut "Alt+Space"`, `max_file_size_mb 100`, `max_pdf_pages 500`,
`index_on_battery false`, `index_cpu_threads cores/2`, `summaries_enabled true` (false on tier 0),
`llm_idle_unload_min 10`, `llm_preload false`, `chunk_target_chars 1600`, `chunk_overlap_chars 240`,
`search_top_k 10`, `rag_top_k 6`, `autostart false`. The shortcut, language and autostart are applied by the frontend/Tauri.
