<div align="center">

# Smart Space · Backend v1

**The local AI engine behind Smart Space, a privacy-first file manager.**<br/>
Search your files by meaning and chat with them. Everything runs on your own computer.

![Python 3.11](https://img.shields.io/badge/python-3.11-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.11x-009688?logo=fastapi&logoColor=white)
![llama.cpp](https://img.shields.io/badge/llama.cpp-b12475-555555)
![LanceDB](https://img.shields.io/badge/LanceDB-vectors-6E56CF)
![SQLite FTS5](https://img.shields.io/badge/SQLite-FTS5-003B57?logo=sqlite&logoColor=white)
![Windows](https://img.shields.io/badge/platform-Windows-0078D6?logo=windows&logoColor=white)
![Tests](https://img.shields.io/badge/tests-131%20passing-2ea44f)

</div>

Point Smart Space at one folder. The backend reads every document, image and scanned page in it, understands
German and English, and answers questions like *"How much is my monthly rent?"* with the exact file and page the
answer came from. No cloud, no account, no telemetry: the only time it goes online is a model download you start.

This repository is the **backend** (a FastAPI "sidecar" process). The desktop app (Tauri v2 + React) talks to it over
HTTP on `127.0.0.1` and starts and stops it together with the window.

> Software engineering capstone project, built from the *SMART SPACE Technical Specification (SDD) v0.1*.

---

## What it does

| | |
|---|---|
| **Reads almost anything** | PDF, Word, PowerPoint, text, Markdown, CSV, plus photos and scanned PDFs through OCR. Payroll-style PDFs that draw their decimal commas as lines are read correctly. |
| **Search by meaning and keyword** | Hybrid search (vectors + BM25, fused with RRF). "rental contract" finds a German *Mietvertrag*, "october" finds *Oktober*, and exact terms like `INV-20417` still match. 50–200 ms. |
| **Chat with your files** | Answers use **only** your documents, with numbered citations to file and page. Questions outside your files get "I could not find it". German and English. |
| **Summaries and tags** | Every file gets a short summary in its own language, a document type (contract, invoice, CV, lecture, …) and tags. |
| **Stays in sync** | A folder watcher picks up new, changed, renamed and deleted files within seconds. |
| **Fits the hardware** | Profiles CPU, RAM and GPU (NVIDIA, AMD, Intel), places the PC in tier 0–4 and recommends models. Every model can still be chosen. |
| **Reuses models already on the PC** | Finds identical model files in other Smart Space folders, Hugging Face, LM Studio or Ollama and links them instead of downloading. |
| **Privacy dashboard** | Shows everything stored, deletes any part of it (index, chats, models, everything) and keeps an audit log. |

## Privacy by design

- Listens only on `127.0.0.1`; every request needs a per-session Bearer token.
- Outbound traffic only for a model download the user starts: HTTPS from allowlisted Hugging Face hosts,
  verified against a pinned SHA-256 before use.
- **Your files are never changed, moved or deleted.** A dedicated test wraps every delete call while all purge
  scopes run and checks that nothing in the workspace is touched.
- Logs never contain file contents or search queries.

## How it works

```
 Desktop app (Tauri) ──HTTP + SSE, Bearer token──▶  FastAPI backend (127.0.0.1)
                                                    │
   folder ─▶ scanner + watcher ─▶ job queue ─▶ extract (PDF/Office/OCR) ─▶ chunk ─▶ embed ─▶ LanceDB
                                                    │                                     └─▶ SQLite FTS5
                                                    ├─▶ summarizer ─┐
   question ─▶ hybrid search (RRF) ─▶ RAG prompt ─▶ llama-server (llama.cpp, CUDA / Vulkan / CPU)
                                                    └─▶ streamed answer with [n] citations (SSE)
```

## Quick start (Windows)

Requirements: Python 3.11 via [uv](https://docs.astral.sh/uv/), PowerShell.

```powershell
cd backend
uv sync --python 3.11
powershell -ExecutionPolicy Bypass -File ..\scripts\fetch_llama_server.ps1 -Variants cpu,vulkan,cuda
powershell -ExecutionPolicy Bypass -File ..\scripts\dev_backend.ps1
```

The API runs at `http://127.0.0.1:8765/api/v1` with token `dev`; Swagger UI at `http://127.0.0.1:8765/docs`.

```bash
H="Authorization: Bearer dev"; B=http://127.0.0.1:8765/api/v1
curl -s -X POST -H "$H" $B/models/emb-mini/download          # search model, 0.25 GB
curl -s -X POST -H "$H" $B/models/llm-3b/download            # language model, 2.1 GB
curl -s -X PUT  -H "$H" -H "Content-Type: application/json" -d '{"root_path":"C:/path/to/folder"}' $B/workspace
curl -s -X POST -H "$H" $B/index/start
curl -s -X POST -H "$H" -H "Content-Type: application/json" -d '{"query":"rental contract"}' $B/search
```

A demo folder with German and English documents, photos and a scanned PDF:
`powershell -ExecutionPolicy Bypass -File ..\scripts\make_demo_workspace.ps1`.

## API

| Area | Endpoints |
|---|---|
| System | `GET /health`, `GET /system/info`, `GET /system/hardware`, `POST /system/shutdown` |
| Models | `GET /models/catalog`, `GET /models/recommendations`, `POST /models/{id}/download`, `POST /models/{id}/activate`, `GET /models/downloads/events` (SSE), `GET /models/llm/status` |
| Workspace | `GET/PUT/DELETE /workspace`, `GET /workspace/stats`, exclusions |
| Files | `GET /files`, `GET /files/{id}`, `/chunks`, `/preview`, `/reindex`, `/tags`, `DELETE /files/{id}/index` |
| Index | `POST /index/start · pause · resume · retry-failed`, `GET /index/status`, `GET /index/events` (SSE) |
| Search | `POST /search` (full and quick mode, filters, highlighted snippets) |
| Chat | `/chat/sessions`, `POST /chat/sessions/{id}/messages` (SSE: `meta → files → token… → sources → done`) |
| Privacy | `GET /privacy/overview · audit · export`, `POST /privacy/purge` |
| Settings | `GET/PUT /settings` |

Full guide for frontend developers: [docs/API.md](docs/API.md) · OpenAPI contract: [docs/openapi.json](docs/openapi.json).

## Models

| Id | Model | Role | Download | License | Default on tier |
|---|---|---|---|---|---|
| `emb-mini` | Multilingual MiniLM-L12 v2 (ONNX) | search | 0.25 GB | Apache-2.0 | 0–3 |
| `emb-large` | Multilingual E5 Large (ONNX) | search | 2.1 GB | MIT | 3–4 |
| `llm-1b` | Qwen2.5 1.5B Instruct Q4_K_M | language | 1.1 GB | Apache-2.0 | 0 |
| `llm-3b` | Qwen2.5 3B Instruct Q4_K_M | language | 2.1 GB | Qwen research (non-commercial) | 1–2 |
| `llm-7b` | Qwen2.5 7B Instruct Q4_K_M | language | 4.7 GB | Apache-2.0 | 3 |
| `llm-14b` | Qwen2.5 14B Instruct Q4_K_M | language | 9.0 GB | Apache-2.0 | 4 |
| `ocr-rapid` | RapidOCR PP-OCR (ONNX) | text recognition | bundled | Apache-2.0 | all |

Downloads are pinned to a Hugging Face commit and checked with SHA-256. On NVIDIA GPUs the CUDA build of llama.cpp is
used automatically (Vulkan for AMD/Intel, CPU as fallback).

## Quality and tests

```powershell
cd backend
uv run --python 3.11 pytest -q          # 131 tests, offline (hash embedder + stub LLM), ~2 min
```

Measured on an i5-12450H with an RTX 5060 Ti:

- **Search:** recall@3 **0.97**, recall@10 **1.00** on 32 German/English queries (exact, paraphrase, cross-lingual,
  typos, OCR), 50–200 ms per query.
- **Chat:** 18/18 demo questions answered with the right source; questions outside the files are refused.
- **Speed:** Qwen2.5 3B fully on the GPU, ~1 s per answer; a 17-file demo folder is indexed and summarized in ~50 s.
- In a side-by-side test Qwen2.5 3B answered payslip questions better than 7B (12/20 vs 8/20) and 2–3× faster,
  so 3B stays the default for 4 GB graphics cards.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `SS_PORT` | 8765 | API port (127.0.0.1 only) |
| `SS_TOKEN` | random | Bearer token (`dev` in development) |
| `SS_DATA_DIR` | `%APPDATA%\SmartSpace` | database, vectors, models, logs |
| `SS_LLAMA_SERVER` | auto | explicit `llama-server.exe` |
| `SS_MODEL_DIRS` | – | extra folders to look in for models already on the PC |
| `SS_DEV` | 0 | 1 opens `/docs` without a token |

All variables, the layout, packaging (PyInstaller) and design decisions: [backend/README.md](backend/README.md).

## Project layout

```
backend/
  app/
    api/            REST routers (system, models, workspace, files, index, search, chat, privacy, settings)
    services/       indexer, extractors, chunker, embedder, vectorstore, search, chat, summarizer,
                    llm_runner, model_manager, downloads, local_models, hardware, watcher, purge, …
    prompts/        RAG answer, query rewrite, summary and classification prompts
    db/             SQLite schema and migrations (FTS5)
  tests/            pytest suite, fixture workspace generator, stub LLM server, search eval set
scripts/            llama.cpp fetch, dev server, demo workspace, PyInstaller build
docs/               API guide, OpenAPI contract, demo-PC testing guide
```

## Known limitations

- OCR runs on the CPU (~3 s per photo, 10 s+ per scanned page).
- A small language model can still pick the wrong figure from dense tables (for example a year-to-date total
  instead of the monthly amount); every answer shows its source so it can be checked.
- Windows first; macOS/Linux GPU detection is best effort.
