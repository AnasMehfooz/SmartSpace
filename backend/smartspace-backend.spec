# PyInstaller spec for the SMART SPACE backend sidecar (SDD 14, step 1). One-folder mode.
#   uv run --python 3.11 --with pyinstaller pyinstaller smartspace-backend.spec --noconfirm
# Output: dist/smartspace-backend/smartspace-backend.exe (+ _internal/)
from PyInstaller.utils.hooks import collect_all, collect_submodules

datas, binaries, hiddenimports = [], [], []
for pkg in ("lancedb", "lance", "pyarrow", "onnxruntime", "rapidocr_onnxruntime", "fastembed", "tokenizers",
            "lingua", "pymupdf", "uvicorn", "sse_starlette", "pptx", "docx", "charset_normalizer", "pynvml",
            "cpuinfo", "watchdog", "structlog"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

datas += [
    ("app/prompts", "app/prompts"),
    ("app/db/migrations", "app/db/migrations"),
    ("app/models_catalog.json", "app"),
    ("app/download_allowlist.json", "app"),
]
hiddenimports += collect_submodules("app")

a = Analysis(
    ["run_backend.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "matplotlib", "IPython", "pytest", "torch", "tensorflow"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="smartspace-backend", console=True,
          upx=False)
coll = COLLECT(exe, a.binaries, a.datas, upx=False, name="smartspace-backend")
