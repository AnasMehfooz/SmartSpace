# SMART SPACE backend: test guide for the demo PC

**Who this is for:** Developer A (frontend) and the AI coding assistant (Claude Code) helping them on the demo
laptop. If you are the assistant: read this whole file before running anything, follow the steps in order, run
every command yourself in PowerShell, compare the output with the **Expected** lines, and fill in the report in
section 10 at the end.

**Goal:** prove that the backend works on the demo laptop before the MVP presentation (23 Nov 2026), choose the
GPU build, prepare the demo data, and connect the frontend. This covers MVP plan tasks A2.1, A2.2, A2.5 and A2.7.

## 0. Rules

- **Do not change backend code** (`backend/`, `scripts/`) to make a check pass. If something fails, stop, collect
  the evidence (section 9) and report it to Developer B (Anas), who owns the backend.
- Do not commit to `main`. Results go on a branch named `test/demo-pc-results` (section 10) and into a pull request.
- Never delete or move files inside the workspace folder; the backend only reads them.
- Everything runs offline after the downloads in steps 2 and 4.
- Use **Windows PowerShell** from the repository root unless a step says otherwise.

## 1. Facts you can rely on

| Item | Value |
|---|---|
| Repo | `https://github.com/AnasMehfooz/Smart-Space`, branch `main` |
| API base | `http://127.0.0.1:8765/api/v1` |
| Token (dev mode) | `dev` → header `Authorization: Bearer dev` (only `GET /health` needs no token) |
| Swagger UI | `http://127.0.0.1:8765/docs` (click *Authorize*, enter `dev`) |
| App data folder for the demo | `C:\SmartSpaceDemo` (database, vectors, models, logs) |
| Demo documents folder | `C:\SmartSpaceDemoFiles` (17 test files; keep it **outside** the data folder) |
| Logs | `C:\SmartSpaceDemo\logs\backend.log` and `llama-server.log` |
| API reference | [docs/API.md](API.md) (human guide) and [docs/openapi.json](openapi.json) (contract) |
| Demo laptop | 16 GB RAM, NVIDIA RTX 3050 Laptop GPU with 4 GB VRAM |
| Expected hardware tier | **2** → models `emb-mini` (252 MB) + `llm-3b` (2.1 GB, Qwen2.5 3B), context 6144 |

## 2. Prerequisites and setup (once)

1. Check the tools. Install anything missing, then open a **new** PowerShell window.

   ```powershell
   git --version           # any recent version
   uv --version            # if missing: winget install --id astral-sh.uv -e
   nvidia-smi              # must list the RTX 3050; if it fails, update the NVIDIA driver
   ```

2. Get the code and the Python environment (uv downloads Python 3.11 itself).

   ```powershell
   git clone https://github.com/AnasMehfooz/Smart-Space.git   # or: git pull, if already cloned
   cd Smart-Space
   git checkout main
   cd backend
   uv sync --python 3.11
   cd ..
   ```

   **Expected:** `uv sync` ends without errors.

3. Download the pinned llama.cpp server builds (CPU, Vulkan and CUDA; about 700 MB, SHA-256 checked).

   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\fetch_llama_server.ps1 -Variants cpu,vulkan,cuda
   Get-ChildItem backend\bin\llama
   ```

   **Expected:** folders `cpu`, `cuda`, `vulkan`, each containing `llama-server.exe`.

4. Run the automated tests (offline, about 2 minutes).

   ```powershell
   cd backend
   uv run --python 3.11 pytest -q
   cd ..
   ```

   **Expected:** `91 passed` (the number can only go up). Any failure: stop and report it.

5. Create the demo documents folder.

   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\make_demo_workspace.ps1 -Dest C:\SmartSpaceDemoFiles
   ```

   **Expected:** subfolders `Finance`, `Housing`, `Images`, `Misc`, `Notes`, `Personal`, `Scans`, `University`,
   `Work` (plus `node_modules` and `.hidden`, which the backend must ignore).

## 3. Start the backend

**Plug the laptop into power.** On battery, indexing pauses by design (`paused_reason: "battery"`).

Open a separate PowerShell window for the server and leave it running:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\dev_backend.ps1 -DataDir C:\SmartSpaceDemo
```

**Expected:** the line `SMART SPACE backend -> http://127.0.0.1:8765  (token: dev, ...)` and no traceback.
Stop it later with Ctrl+C.

In your working PowerShell window, paste this helper block once per window. It sends the token, sends and reads
UTF-8 correctly (Windows PowerShell 5.1 otherwise garbles umlauts), and parses JSON. **Keep these exact variable
names:** PowerShell variable names ignore case, so a variable called `$h` would overwrite `$H`.

```powershell
$Api     = "http://127.0.0.1:8765/api/v1"
$Auth    = @{ Authorization = "Bearer dev" }
$LlamaLog = "C:\SmartSpaceDemo\logs\llama-server.log"

function Invoke-SS {
    param([string]$Method = "GET", [string]$Path, $Body = $null, [switch]$Raw)
    $p = @{ Uri = "$Api$Path"; Method = $Method; Headers = $Auth; UseBasicParsing = $true; TimeoutSec = 600 }
    if ($null -ne $Body) {
        $p.ContentType = "application/json; charset=utf-8"
        $p.Body = [Text.Encoding]::UTF8.GetBytes(($Body | ConvertTo-Json -Depth 6 -Compress))
    }
    try { $r = Invoke-WebRequest @p } catch { throw "HTTP error: $($_.ErrorDetails.Message)" }
    $text = [Text.Encoding]::UTF8.GetString($r.RawContentStream.ToArray())
    if ($Raw) { $text } elseif ($text) { $obj = $text | ConvertFrom-Json; $obj }   # $obj unrolls JSON arrays
}

function Ask-SS {
    # Asks one chat question in a new session; returns the answer, citations and total time.
    param([string]$Question)
    $sid = (Invoke-SS POST "/chat/sessions" @{}).id
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $sse = Invoke-SS POST "/chat/sessions/$sid/messages" @{ content = $Question } -Raw
    $events = @(); $name = $null
    foreach ($line in $sse -split "`r?`n") {
        if ($line.StartsWith("event:")) { $name = $line.Substring(6).Trim() }
        elseif ($line.StartsWith("data:")) { $events += [pscustomobject]@{ event = $name; data = ($line.Substring(5) | ConvertFrom-Json) } }
    }
    $done = ($events | Where-Object event -eq "done").data
    [pscustomobject]@{
        answer     = (($events | Where-Object event -eq "token") | ForEach-Object { $_.data.t }) -join ""
        sources    = (($events | Where-Object event -eq "sources").data.citations | ForEach-Object { "[$($_.n)] $($_.rel_path)" }) -join "; "
        error      = ($events | Where-Object event -eq "error").data.message
        seconds    = [math]::Round($sw.Elapsed.TotalSeconds, 1)   # search + prompt + generation
        tokens_out = $done.tokens_out
    }
}

function Get-LlmSpeed {
    # Average speed of the last N answers, as measured by llama-server itself.
    param([int]$Last = 4)
    $log = Get-Content $LlamaLog -Tail 3000
    $gen = $log | Select-String '\|\s+eval time.*?([\d.]+) tokens per second' | Select-Object -Last $Last |
        ForEach-Object { [double]$_.Matches[0].Groups[1].Value }
    $pp  = $log | Select-String 'prompt eval time.*?([\d.]+) tokens per second' | Select-Object -Last $Last |
        ForEach-Object { [double]$_.Matches[0].Groups[1].Value }
    [pscustomobject]@{
        answers          = @($gen).Count
        generation_tok_s = [math]::Round(($gen | Measure-Object -Average).Average, 1)
        prompt_tok_s     = [math]::Round(($pp | Measure-Object -Average).Average, 1)
    }
}

Invoke-RestMethod "$Api/health"
```

**Expected:** `status: ok`, `ready: True`, `db_ok: True`, `llm_state: stopped`. If `ready` is `False`, wait a few
seconds and repeat.

## 4. Full flow (task A2.1 and A2.7)

The data folder is `C:\SmartSpaceDemo`, so the models downloaded here stay there for the demo (A2.7).

1. **Hardware and recommendation**

   ```powershell
   Invoke-SS POST "/system/hardware/scan" | Out-Null
   $hw = Invoke-SS GET "/system/hardware"
   $hw | Select-Object tier, cpu_name, ram_total_mb, battery
   $hw.gpus | Format-Table name, vendor, vram_mb, vram_free_mb, backend, integrated, usable
   Invoke-SS GET "/models/recommendations" | ConvertTo-Json -Depth 4
   ```

   **Expected:** `tier` = 2; the RTX 3050 listed with `vendor nvidia`, about 4096 MB, `usable True`; the Intel iGPU
   (if listed) `integrated True`, `usable False`. Recommendation: `embedding emb-mini`, `llm llm-3b`,
   `ocr ocr-rapid`, `ctx_cap 6144`. If the tier is not 2, record it and continue with the recommended models.

2. **Download the models** (about 2.4 GB; the first model of each role activates itself when it finishes).

   ```powershell
   Invoke-SS POST "/models/emb-mini/download" | Out-Null
   Invoke-SS POST "/models/llm-3b/download"   | Out-Null
   do {
       Start-Sleep 10
       $cat = Invoke-SS GET "/models/catalog" | Where-Object { $_.id -in "emb-mini", "llm-3b" }
       $cat | ForEach-Object { "{0}: {1} {2}" -f $_.id, $_.status, ($(if ($_.download) { "$([math]::Round($_.download.bytes_done / 1MB)) MB" })) }
   } until (@($cat | Where-Object status -eq "installed").Count -eq 2 -or ($cat | Where-Object status -eq "failed"))
   Invoke-SS GET "/models/catalog" | Where-Object is_active | Format-Table id, role, status, is_active
   ```

   **Expected:** both `installed`, and `emb-mini`, `llm-3b` and `ocr-rapid` are active. A `failed` status: check
   the internet connection and repeat the download call (downloads resume).

3. **Set the workspace and index it**

   ```powershell
   Invoke-SS PUT "/workspace" @{ root_path = "C:\SmartSpaceDemoFiles" } | ConvertTo-Json -Depth 3
   Invoke-SS POST "/index/start" | Out-Null
   $t0 = Get-Date
   do {
       Start-Sleep 3
       $st = Invoke-SS GET "/index/status"
       "{0} indexed {1}/{2} skipped {3} errors {4} summaries {5} pending {6} paused {7}" -f $st.state, $st.indexed, $st.total, $st.skipped, $st.errors, $st.summaries_done, $st.summaries_pending, $st.paused_reason
   } until ($st.state -eq "idle" -and $st.pending -eq 0 -and $st.processing -eq 0 -and $st.summaries_pending -eq 0)
   "took {0:N0} s" -f ((Get-Date) - $t0).TotalSeconds
   ```

   **Expected:** ends with `total 17`, `indexed 15`, `skipped 2`, `errors 0`, `summaries 15`. Skipped are
   `Misc/empty.txt` (empty) and `Misc/song.mp3` (unsupported). Time: about 1–3 minutes (OCR on images, then one
   AI summary per file). If it stays `paused` with `battery`, plug in the charger.
   If `PUT /workspace` returns `WORKSPACE_EXISTS`, a different folder was set before; repeat it with
   `@{ root_path = "C:\SmartSpaceDemoFiles"; replace = $true }`.

4. **File list with AI fields** (new in this version)

   ```powershell
   (Invoke-SS GET "/files?limit=100").items | Format-Table rel_path, status, skip_reason, doc_type, @{n="tags"; e={$_.tags -join ", "}} -AutoSize
   (Invoke-SS GET "/files?q_name=Mietvertrag").items | Select-Object rel_path, summary_short, tags | Format-List
   ```

   **Expected:** 17 rows. Indexed files have a `doc_type` (contract, invoice, cv, notes, ...), a few `tags` and a
   `summary_short` of at most 160 characters. Skipped files have `tags` empty and no summary.

5. **Search**

   ```powershell
   foreach ($q in "rental contract", "Rechnung für den Laptop", "how much was the cappuccino", "Haftpflichtversicherung") {
       $r = Invoke-SS POST "/search" @{ query = $q; limit = 5 }
       "{0,-32} {1,4} ms  -> {2}" -f $q, $r.took_ms, (($r.results | Select-Object -First 3).rel_path -join " | ")
   }
   ```

   **Expected:** the first result is `Housing/Mietvertrag_2024.pdf`, `Finance/Invoice_Laptop_2024.pdf`,
   `Images/receipt_coffee.png` and `Scans/Scanned_Insurance_Letter.pdf`; each search under about 500 ms.

6. **Chat** (the first question also loads the language model, so it is slower)

   ```powershell
   Ask-SS "How much is my monthly rent and when did the lease start?" | Format-List
   Ask-SS "Wie hoch ist die Kaution?" | Format-List
   Invoke-SS GET "/models/llm/status" | Format-List
   ```

   **Expected:** the answer says the rent is 950 Euro and the lease started on 1 March 2024, with a citation
   `[1] Housing/Mietvertrag_2024.pdf`; `error` is empty. LLM status: `state ready`, `model_id llm-3b`, `ctx 6144`,
   `gpu_layers 99` (the whole model is on the GPU; a smaller number means part of it runs on the CPU, record it),
   `backend cuda`, `device` the RTX 3050 (for example `CUDA0`).

7. **Live events** (optional; checks what the Home screen will receive)

   ```powershell
   curl.exe -N -H "Authorization: Bearer dev" http://127.0.0.1:8765/api/v1/index/events
   ```

   In your working window (helper loaded), re-index one file:
   `Invoke-SS POST "/files/$((Invoke-SS GET '/files?q_name=Pancake').items[0].id)/reindex"`.
   **Expected** in the stream, in this order: `file_processing` with `{"file_id": ..., "rel_path":
   "Notes/Pancake_Recipe.md"}`, `file_indexed`, `progress`, then `summary_done`. Stop with Ctrl+C.

## 5. GPU build comparison (task A2.2)

The backend picks the build by itself: CUDA if `backend\bin\llama\cuda` exists and the GPU is NVIDIA, else Vulkan,
else CPU. Compare both on the RTX 3050 and keep the better one. Run each round from a fresh backend start.

**Round A: CUDA.** Restart the backend (Ctrl+C in the server window, start it again with the same command), paste
the helper block again if you opened a new window, then:

```powershell
Invoke-SS POST "/models/llm/start" | Out-Null
do { Start-Sleep 2; $llm = Invoke-SS GET "/models/llm/status" } until ($llm.state -in "ready", "error")
$llm | Format-List state, backend, device, gpu_layers, ctx, vram_used_mb, error
nvidia-smi --query-gpu=name,memory.used,memory.total --format=csv
$runs = foreach ($q in "How much is my monthly rent?", "Summarize the kickoff meeting action items.", "What does the insurance letter say about liability?", "Wie hoch war die Steuererstattung?") { Ask-SS $q }
$runs | Format-Table seconds, tokens_out, error, sources -AutoSize -Wrap
Get-LlmSpeed -Last 4
```

**Round B: Vulkan.** Stop the backend, hide the CUDA build, start again, repeat the same commands:

```powershell
Rename-Item backend\bin\llama\cuda cuda.off
```

**Expected for both rounds:** `state ready`, no `error`, no "out of memory" in
`C:\SmartSpaceDemo\logs\llama-server.log`, every answer cites a file, `vram_used_mb` under about 3500 (the model
plus a 6144 context must fit into 4 GB).

**Round C: CPU fallback.** Also hide Vulkan (`Rename-Item backend\bin\llama\vulkan vulkan.off`), restart, run one
`Ask-SS` question. **Expected:** `backend cpu`, `gpu_layers 0`, a correct but slower answer. This proves the demo
still works if the GPU fails. (The automatic "half the layers, then CPU" retry after a GPU out-of-memory error is
covered by the unit tests.)

**Decide and restore:** pick the build with the higher `generation_tok_s` and no errors (CUDA wins ties). For
reference, llm-3b reached about 24 tokens/s on a 2 GB MX550 (partly offloaded) and 83 tokens/s on an RTX 5060 Ti. Then rename the
folders back so that only what you want is active:

```powershell
Rename-Item backend\bin\llama\vulkan.off vulkan -ErrorAction SilentlyContinue
# CUDA chosen:
Rename-Item backend\bin\llama\cuda.off cuda -ErrorAction SilentlyContinue
# Vulkan chosen: leave the CUDA folder as cuda.off
```

Restart the backend once more and confirm `/models/llm/status` shows the chosen `backend`.

## 6. Search quality and demo queries (task A2.5)

With the backend running and the demo folder indexed:

```powershell
cd backend
uv run --python 3.11 python -m tests.search_eval --base http://127.0.0.1:8765 --token dev
cd ..
```

**Expected:** `recall@3` about 0.95 or higher, `recall@10` 1.0 (developer PC: 0.97 and 1.00).

Avoid typo queries in the demo (`mietvertrg` did not rank the contract first in a check). Then confirm the
candidate demo queries below. Each one must put the expected file **first** in search, and the
chat questions must cite it. Keep 5–6 that work every time (mix German and English), and write them into the report.

| Query (search) | Expected first result | Shows |
|---|---|---|
| `rental contract` | `Housing/Mietvertrag_2024.pdf` | English query finds a German file |
| `Rechnung für den Laptop` | `Finance/Invoice_Laptop_2024.pdf` | German query finds an English file |
| `how much was the cappuccino` | `Images/receipt_coffee.png` | text read from a photo (OCR) |
| `Haftpflichtversicherung` | `Scans/Scanned_Insurance_Letter.pdf` | text from a scanned PDF |
| `vacation in Florence by train` | `Notes/Urlaubsplanung.md` | English query, German notes |
| `INV-20417` | `Finance/Invoice_Laptop_2024.pdf` | exact invoice number |
| `slides about supervised learning` | `University/Machine_Learning_Intro.pptx` | meaning, not keywords |

| Question (chat) | Expected answer | Cites |
|---|---|---|
| How much is my monthly rent and when did the lease start? | 950 Euro, since 1 March 2024 | `Housing/Mietvertrag_2024.pdf` |
| Who builds the onboarding wizard? | Developer A | `Work/Meeting_Notes_Kickoff.docx` |
| Wie hoch ist die Kaution? | 2.850,00 Euro | `Housing/Mietvertrag_2024.pdf` |
| Wo hat Anna studiert? | TU Berlin (Informatik, 2020–2022) and Universität Hamburg (Wirtschaftsinformatik, 2016–2020) | `Personal/Lebenslauf_Anna_Schmidt.docx` |

A quick check for one query: `(Invoke-SS POST "/search" @{ query = "mietvertrg" }).results[0].rel_path`.

## 7. Prepare the demo state (task A2.7)

- Models live in `C:\SmartSpaceDemo\models`; check with `Get-ChildItem C:\SmartSpaceDemo\models -Recurse | Measure-Object Length -Sum`
  (about 2.4 GB). Do not delete this folder before the presentation.
- Keep `C:\SmartSpaceDemoFiles` indexed. Before the demo, start the backend with the same `-DataDir` and check
  `GET /index/status` shows 15 indexed, then turn off Wi-Fi and repeat one search and one chat question: both must
  work offline.
- Whoever starts the backend for the demo (the dev script or the Tauri app) must use `SS_DATA_DIR=C:\SmartSpaceDemo`,
  or it starts with an empty data folder.

## 8. Connect the frontend (dev mode)

- Run the backend with `scripts\dev_backend.ps1 -DataDir C:\SmartSpaceDemo` (port 8765, token `dev`), and the
  Vite dev server on `http://localhost:5173` (that origin is allowed by CORS; so are the Tauri origins).
- In the frontend, use `VITE_DEV_BACKEND=http://127.0.0.1:8765` and token `dev`; in the packaged app the port and
  token come from Tauri's `get_backend_info()`.
- Every request sends `Authorization: Bearer <token>`. Read the event streams (`/index/events`,
  `/models/downloads/events`, chat messages) with `fetch` and a stream reader so the header can be sent; the
  example reader is in [docs/API.md](API.md#server-sent-events).
- Errors always look like `{"error": {"code", "message", "details"}}`; show `message` to the user.
- New in this version: `file_processing {file_id, rel_path}` on `/index/events` for the "scanning now" line, and
  `tags` plus `summary_short` on every `GET /files` item for the Home list.

## 9. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `uv` not found after install | open a new PowerShell window |
| Indexing stays `paused`, `paused_reason battery` | plug in the charger (or `Invoke-SS PUT "/settings" @{ index_on_battery = $true }`) |
| `paused_reason cpu` | another program uses the CPU heavily; close it, indexing resumes by itself |
| `tier` lower than 2, or the RTX 3050 missing in `gpus` | update the NVIDIA driver, plug in power, run `Invoke-SS POST "/system/hardware/scan"` |
| `LLM_START_FAILED` / llama-server not found | step 2.3 not done, or both GPU folders renamed; check `backend\bin\llama` |
| `LLM_START_FAILED` with "out of memory" | close other GPU programs (browser with video, games); record it in the report |
| Chat answers without citations or says it does not know | check `/index/status` (15 indexed) and that `emb-mini` is active |
| Garbled umlauts in output | use the `Invoke-SS` helper, not plain `Invoke-RestMethod` |
| Port 8765 busy | an old backend still runs: close its window (find the process with `Get-NetTCPConnection -LocalPort 8765`) |

When something fails, collect: the command and its full output, the last 50 lines of
`C:\SmartSpaceDemo\logs\backend.log` and `llama-server.log` (`Get-Content <file> -Tail 50`), and the output of
`Invoke-SS GET "/system/hardware"` and `nvidia-smi`. Do not include the contents of personal documents.

## 10. Report

Create `docs/DEMO_PC_RESULTS.md` on a branch and open a pull request for Developer B:

```powershell
git checkout -b test/demo-pc-results
# write docs/DEMO_PC_RESULTS.md using the template below
git add docs/DEMO_PC_RESULTS.md
git commit -m "test(demo): results from the demo PC"
git push -u origin test/demo-pc-results
```

Template:

```markdown
# Demo PC results (date)

## Machine
- CPU / RAM / GPU / driver:
- tier and recommended models:

## A2.1 full flow
- pytest: N passed / failures:
- indexing: total / indexed / skipped / errors / summaries, time:
- search checks (4): pass/fail per query, took_ms:
- chat: answer + citation OK? first-answer seconds:

## A2.2 GPU build
| build | gpu_layers | device | vram_used_mb | generation_tok_s | prompt_tok_s | errors |
|---|---|---|---|---|---|
| CUDA | | | | | | |
| Vulkan | | | | | | |
| CPU | | | | | | |
- chosen build and why:

## A2.5 search eval and demo queries
- recall@3 / recall@10:
- final demo queries (5-6) and expected file:

## A2.7 demo state
- models in C:\SmartSpaceDemo (size):
- offline check (search + chat with Wi-Fi off):

## Problems
- (command, output, log excerpt)
```
