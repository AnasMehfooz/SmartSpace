# SMART SPACE backend (FastAPI sidecar)

Everything runs on the user's machine: indexing, OCR, embeddings, hybrid search, summaries and RAG chat.
The only outbound traffic is the model download the user clicks (allowlisted Hugging Face hosts).
Implements SDD sections 4 to 10, 13 and 15 for the MVP cut line (16.3).

## Quick start (Windows, dev mode)

```powershell
cd backend
uv sync --python 3.11                                                    # Python 3.11 venv + pinned deps (uv.lock)
powershell -ExecutionPolicy Bypass -File ..\scripts\fetch_llama_server.ps1 -Variants cpu,vulkan,cuda   # pinned llama.cpp b12475, SHA-256 checked
powershell -ExecutionPolicy Bypass -File ..\scripts\dev_backend.ps1          # http://127.0.0.1:8765, token "dev", Swagger at /docs
```

Then, with the server running (PowerShell `curl.exe`, or Swagger at http://127.0.0.1:8765/docs → *Authorize* → `dev`):

```bash
H="Authorization: Bearer dev"; B=http://127.0.0.1:8765/api/v1
curl -s $B/health                                                  # no auth
curl -s -H "$H" $B/system/hardware                                 # tier 0..4
curl -s -H "$H" $B/models/recommendations
curl -s -X POST -H "$H" $B/models/emb-mini/download                # 252 MB, auto-activates
curl -s -X POST -H "$H" $B/models/llm-3b/download                  # 2.1 GB, auto-activates
curl -s -X PUT -H "$H" -H "Content-Type: application/json" -d '{"root_path":"C:/path/to/demo_workspace"}' $B/workspace
curl -s -X POST -H "$H" $B/index/start
curl -s -H "$H" $B/index/status
curl -s -X POST -H "$H" -H "Content-Type: application/json" -d '{"query":"rental contract"}' $B/search
```

A demo folder with German/English documents, images with text and a scanned PDF:
`powershell -ExecutionPolicy Bypass -File ..\scripts\make_demo_workspace.ps1` (writes `demo_workspace/`).

The frontend teammate's guide (flows, SSE events, examples) is [../docs/API.md](../docs/API.md);
the contract is [../docs/openapi.json](../docs/openapi.json) (regenerate with `uv run python export_openapi.py`).

## What works

| SDD feature | Status |
|---|---|
| Token auth (Bearer, `?token=` for `/events`), 127.0.0.1 only, CORS for Tauri + `localhost:5173` | done |
| SQLite schema + FTS5 (`unicode61 remove_diacritics 2`), WAL, one writer, versioned migrations | done |
| Hardware profiler (CPU/AVX2, RAM, NVIDIA via NVML, AMD/Intel via WMI + registry, disk, battery) and tiers 0-4 | done |
| Model catalog (data-only JSON, pinned HF commits, SHA-256), recommendations + warnings | done |
| Download manager: queue, Range resume, 500 ms progress SSE, SHA-256, atomic move, host allowlist per redirect hop, cancel | done |
| llama-server runner: lazy start, CUDA > Vulkan > CPU, `-ngl` formula, best-GPU device pick, half-layers then CPU fallback, idle unload, Windows Job Object | done |
| Scanner (iterative, exclusions, hidden, symlink/junction safe), change detection (size/mtime then xxh3_128), move detection | done |
| Extractors: PDF (+ OCR for pages < 20 chars), DOCX (tables, headers), PPTX (slides, notes), TXT/MD/CSV/JSON/LOG, images (RapidOCR) | done |
| Chunker (paragraph > sentence > word, 1600/240, page ranges), language detection de/en (lingua) | done |
| Job queue in SQLite: priorities 1/5/6/8, retry 5 s/30 s/120 s, crash recovery, pause/resume, auto-pause on battery / CPU > 85 % | done |
| Watcher (watchdog, 2 s debounce, moves keep vectors, locked files retried) | done |
| Embeddings (fastembed ONNX, local files only) + LanceDB table per model, IVF_PQ index after 5,000 rows | done |
| Hybrid search: vector + BM25, RRF (k = 60), filename bonus 0.02, filters, `<mark>` snippets, quick mode | done |
| Summaries + doc type + tags (JSON-schema mode, map-reduce for long files), summary chunk embedded | done |
| Chat (RAG): query rewrite, budgeted prompt, `[n]` citations, SSE `meta/files/token/sources/done/error`, stop, search-only fallback | done |
| Privacy: overview, purge (index/file/chats/models/everything, `confirm`), audit log, export | done |
| Settings (typed validation), exclusions, file tree/list/detail/chunks/preview/tags/reindex/remove | done |
| Logging: structlog JSON, rotating 5 x 5 MB, no contents or queries | done |
| PyInstaller one-folder spec + build script | see [Packaging](#packaging) |

Measured on the dev laptop (i5-12450H, 16 GB, RTX 5060 Ti 16 GB): search 50-200 ms; fixture workspace (17 files incl.
4 OCR'd images/scans) indexed in ~35 s; Qwen2.5-3B fully on GPU, ~83 tokens/s, summaries ~2 s per file, first chat
answer ~3 s; search eval **recall@3 0.97, recall@10 1.00** with `emb-mini` (target recall@10 ≥ 0.85).

## Layout

```
app/
  main.py            app factory, middleware (auth, request log, CORS), lifespan
  __main__.py        `python -m app` / PyInstaller entry (uvicorn on 127.0.0.1)
  config.py          SS_* env vars, data dir, resource paths
  context.py         builds and wires all services, crash recovery, shutdown
  security.py        Bearer token ASGI middleware
  errors.py          {"error": {code, message, details}} + handlers
  db/                Database (writer lock + per-thread readers), migrate.py, migrations/0001_initial.sql
  api/               routers: system, models, workspace, files, index, search, chat, privacy, settings
  schemas/           Pydantic request/response models (the OpenAPI contract)
  services/
    hardware.py  catalog.py  model_manager.py  downloads.py  llm_runner.py  llm_client.py
    embedder.py  vectorstore.py  extractors/  chunker.py  language.py
    indexer.py   watcher.py  summarizer.py  search.py  chat.py  files.py  workspace.py  purge.py  settings.py  events.py
  prompts/           summarize.txt (s2), summarize_block.txt, rag_answer.txt, query_rewrite.txt (q1)
  models_catalog.json, download_allowlist.json
tests/               pytest suite, fixture generator, stub LLM, search eval set
```

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `SS_PORT` | 8765 | API port (127.0.0.1 only) |
| `SS_TOKEN` | random (printed at start) | Bearer token; use `dev` in dev mode |
| `SS_DATA_DIR` | `%APPDATA%\SmartSpace` | DB, vectors, models, logs, tmp |
| `SS_LLAMA_PORT` | random free port | llama-server port |
| `SS_RESOURCE_DIR` | `app/` | folder with `models_catalog.json` / `download_allowlist.json` (Tauri resources) |
| `SS_LOG_LEVEL` | INFO | DEBUG adds job tracebacks |
| `SS_DEV` | 0 | 1 enables `/docs` without a token |
| `SS_LLAMA_SERVER` | auto | explicit llama-server.exe (else `bin/llama/{cuda,vulkan,cpu}/`, next to the exe, or PATH) |
| `SS_LLM_EXTERNAL_URL` | - | use an already running OpenAI-compatible server instead of spawning llama-server |
| `SS_MODEL_DIRS` | - | extra folders (`;`-separated) to look in for models that are already on the PC |
| `SS_LOCAL_MODEL_SEARCH` | 1 | 0 = only search `SS_MODEL_DIRS` for existing models, not the default places |

## GPU build choice

On NVIDIA GPUs use the **CUDA** build (`fetch_llama_server.ps1 -Variants cpu,vulkan,cuda`); the runner prefers it
automatically. Measured with Qwen2.5 3B on the demo laptop's RTX 3050 4 GB: CUDA 72 tokens/s (prompt 2,510 t/s),
Vulkan 58 tokens/s (prompt 1,611 t/s, plus a 15 s first-answer warm-up), CPU 18 tokens/s. On a new GPU generation
(RTX 50-series) the very first CUDA start can take over a minute while the driver compiles kernels; GPU starts
therefore get 300 s before they count as failed (CPU: 60 s). Later starts take a few seconds.

## Models already on the PC

Before downloading, the backend looks for an identical copy of each model file (same name and size, then the
SHA-256 is verified) in other Smart Space data folders (`%APPDATA%\SmartSpace`, `C:\SmartSpaceDemo`), the Hugging
Face cache, LM Studio's folders, Ollama's blob store, `Downloads` and `SS_MODEL_DIRS`. A match is hard-linked (same
drive, no extra space) or copied into the data folder; the original is never changed. `GET /models/catalog` shows it
as `local_copy`, and `POST /models/{id}/download` then installs it without using the internet.

## Tests

```powershell
uv run --python 3.11 pytest -q                       # ~3 min, offline (hash embedder + stub LLM server)
uv run --python 3.11 python -m tests.search_eval     # recall@3/@10 against a running backend that indexed demo_workspace
```

`test_units.py` (RRF, FTS escaping, snippets, tiers, `-ngl`, paths, junction escape, exclusion globs), `test_chunker.py`,
`test_extractors.py` (incl. encrypted/corrupt PDFs, OCR), `test_api.py` (auth, error shapes, validation, workspace
rules, CORS, search-only chat, OpenAPI paths), `test_downloads.py` (allowlist, resume, checksum, cancel),
`test_purge_safety.py` (wraps `os.remove/unlink/rmdir`, `shutil.rmtree`, `Path.unlink` and runs every purge scope:
nothing under the workspace root is touched, all source files byte-identical), `test_pipeline.py` (full fixture
workspace: index, search, filters, summaries, chat with citations + query rewrite, watcher create/rename/delete,
embedding switch with `REINDEX_REQUIRED`, purge and re-index).

## Packaging

```powershell
powershell -ExecutionPolicy Bypass -File ..\scripts\build_backend.ps1   # -> backend\dist\smartspace-backend\
```

One-folder PyInstaller build (`smartspace-backend.spec`); llama-server builds are copied into `bin\llama\`.
Tauri spawns `smartspace-backend.exe` with `SS_PORT`, `SS_TOKEN`, `SS_DATA_DIR`, `SS_LLAMA_PORT`, `SS_RESOURCE_DIR`.

Verified on 2026-11-17: the frozen exe starts in ~3.8 s (first launch), searches with fastembed + LanceDB, spawns
llama-server from `bin\llama\vulkan` and answers chat questions with citations; `POST /system/shutdown` leaves no
llama-server process behind. The folder is **1.1 GB uncompressed** (the SDD's 60-90 MB estimate is not realistic with
these libraries): lancedb 233 MB, OpenCV (via RapidOCR) 126 MB, llama.cpp Vulkan + CPU 166 MB, PyMuPDF 99 MB,
lingua 95 MB, pyarrow 87 MB, onnxruntime 49 MB. Ways to trim if installer size matters: drop lingua (the built-in
stopword detector is already the fallback), use `opencv-python-headless`, ship one llama.cpp build and download the
other on demand, and let NSIS compress (LZMA) the rest.

## Deviations from the SDD (deliberate, prototype)

- **Migrations:** plain versioned SQL files + `PRAGMA user_version` instead of SQLAlchemy/Alembic (FTS5 virtual
  table + triggers stay readable; nothing extra to bundle). Schema = SDD 5.1 plus `jobs.run_after` (retry backoff)
  and `files.kind = 'other'` for unsupported files.
- **Catalog:** entries have a `files` list (multi-file ONNX models, split GGUFs), URLs pinned to a Hugging Face commit,
  plus `engine`, `fastembed_name`, `query_prefix`/`passage_prefix`. Qwen2.5-3B is under the Qwen *research*
  (non-commercial) license; the 1.5B/7B/14B models are Apache-2.0.
- **Download allowlist:** Hugging Face now redirects LFS files to `*.hf.co` CDN hosts (e.g. `us.aws.cdn.hf.co`),
  so the allowlist also accepts the `.hf.co` / `.huggingface.co` suffixes (HTTPS only, checked on every hop).
- **Deleted files:** the row is removed (chunks/summary/tags cascade, vectors deleted) instead of keeping
  `status = 'deleted'`; a `file_removed` event is emitted.
- **Exclusions:** adding a pattern removes matching files from the index; `skipped/excluded` is used for
  `DELETE /files/{id}/index`. Built-in patterns cannot be removed (422).
- **Extras:** `abs_path` in file detail (for Tauri `open_file`), PDF page preview, `MODEL_ACTIVE` error code,
  extra SSE events (`file_skipped`, `file_removed`, `summary_failed`, `queued`, `cancelled`), `meta.search_only`
  in chat when no LLM is installed, `SS_DEV`, `SS_LLAMA_SERVER`, `SS_LLM_EXTERNAL_URL`.
- **Summary prompt v`s2`:** the SDD prompt plus short doc_type definitions (the 3B model over-used `invoice`);
  summaries made with an older prompt version are regenerated automatically.
- **CPU auto-pause** measures other processes' load (the indexer's own CPU use is excluded, otherwise it would pause itself).

## Known limitations / next steps

- OCR is CPU-bound (~3 s per image, 10+ s for a 200-DPI scanned page). RapidOCR's bundled PP-OCR models are
  Chinese/English; adding the Latin recognition model to the catalog would improve umlauts in OCR.
- Not implemented: Tesseract fallback, cross-encoder reranker (tier 3-4, optional), LLM image captions,
  `.rtf`. macOS/Linux GPU detection is best-effort (Windows first, as specified).
- Search quality depends on the embedding model: `emb-large` is recommended on tier 3-4 (2.25 GB download).
