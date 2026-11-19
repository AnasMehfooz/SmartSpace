"""Pure-function unit tests: RRF fusion, FTS query building, snippets, tiers, -ngl formula, paths."""

import os
import sys
from pathlib import Path

import pytest

from app.errors import AppError
from app.services.files import short_summary
from app.services.hardware import compute_tier
from app.services.llm_runner import compute_gpu_layers, fallback_layers, kv_cache_mb, parse_devices
from app.services.paths import ExclusionMatcher, is_within, safe_join, validate_workspace_root
from app.services.search import fts_query, keyword_terms, make_snippet, query_terms, rrf_fuse, to_ns

# ---- search helpers -------------------------------------------------------------------------


def test_rrf_matches_formula():
    scores = rrf_fuse([10, 20, 30], [20, 40])
    assert scores[20] == pytest.approx(1 / 62 + 1 / 61)
    assert scores[10] == pytest.approx(1 / 61)
    assert scores[40] == pytest.approx(1 / 62)
    assert max(scores, key=scores.get) == 20  # in both lists beats rank 1 in one list


def test_fts_query_escapes_and_prefixes_last_term():
    assert fts_query(["rental", "contract"]) == '"rental" OR "contract"*'
    assert fts_query([]) is None
    # FTS syntax characters never reach MATCH unquoted
    terms = query_terms('AND OR NOT "x" * (y) near:z')
    assert all('"' not in t and "*" not in t for t in terms)


def test_query_terms_and_stopwords():
    assert query_terms("Where is my Mietvertrag mietvertrag") == ["where", "is", "my", "mietvertrag"]
    assert keyword_terms(["where", "is", "my", "mietvertrag"]) == ["mietvertrag"]
    assert keyword_terms(["the"]) == ["the"]  # never empty


def test_snippet_escapes_html_and_marks_terms():
    s = make_snippet("Die <b>Miete</b> beträgt 950 Euro & mehr", ["miete"])
    assert "<b>" not in s and "&lt;b&gt;" in s
    assert "<mark>Miete</mark>" in s
    long = "x " * 400 + "the rent is 950 EUR " + "y " * 400
    s2 = make_snippet(long, ["rent"], max_len=100)
    assert "<mark>rent</mark>" in s2 and s2.startswith("…") and s2.endswith("…")


def test_to_ns_dates():
    assert to_ns("2024-01-01") < to_ns("2024-01-01", end=True) < to_ns("2024-01-02")
    with pytest.raises(AppError):
        to_ns("not-a-date")


# ---- hardware tiers (SDD 9.2) ---------------------------------------------------------------

def gpu(vram_gb: float, usable: bool = True):
    return {"vram_mb": int(vram_gb * 1024), "usable": usable}


@pytest.mark.parametrize("ram_gb, avx2, gpus, tier", [
    (4, True, [], 0),                   # too little RAM
    (32, False, [gpu(24)], 0),          # no AVX2
    (8, True, [], 1),
    (15.7, True, [], 1),                # "16 GB" laptops report ~15.7 GB -> nominal 16, CPU only -> tier 1
    (16, True, [gpu(4)], 2),
    (8, True, [gpu(4)], 1),             # GPU but not enough RAM for tier 2
    (16, True, [gpu(8)], 3),
    (32, True, [], 3),                  # CPU only with lots of RAM
    (16, True, [gpu(16)], 4),
    (16, True, [gpu(2), gpu(15.9)], 4), # best GPU counts
    (16, True, [gpu(8, usable=False)], 1),  # integrated GPU ignored
])
def test_compute_tier(ram_gb, avx2, gpus, tier):
    assert compute_tier(int(ram_gb * 1024), avx2, gpus) == tier


# ---- llama-server GPU layers (SDD 9.3) ------------------------------------------------------

def test_kv_cache_formula():
    # Qwen2.5-3B @ 6144 ctx: 6144 * 36 * 2 * 2 * 128 * 2 / 1e6
    assert kv_cache_mb(6144, 36, 2, 128) == pytest.approx(226.5, rel=1e-3)


def test_gpu_layers_full_offload_when_it_fits():
    assert compute_gpu_layers(vram_free_mb=15000, model_size_mb=2000, n_layers=36, kv_mb=230) == 99


def test_gpu_layers_partial_and_zero():
    # 4 GB card, 7B model (4466 MB): floor(28 * (4096 - 230 - 300) / 4466) = 22
    assert compute_gpu_layers(4096, 4466, 28, 230) == 22
    assert compute_gpu_layers(300, 4466, 28, 230) == 0
    assert compute_gpu_layers(0, 4466, 28, 230) == 0


def test_fallback_sequence():
    assert fallback_layers(99, 36) == [99, 18, 0]
    assert fallback_layers(22, 28) == [22, 11, 0]
    assert fallback_layers(0, 28) == [0]


def test_parse_list_devices():
    out = """load_backend: loaded Vulkan backend
Available devices:
  Vulkan0: NVIDIA GeForce RTX 5060 Ti (16266 MiB, 15274 MiB free)
  Vulkan1: Intel(R) UHD Graphics (8045 MiB, 7387 MiB free)
"""
    devs = parse_devices(out)
    assert [d["name"] for d in devs] == ["Vulkan0", "Vulkan1"]
    assert devs[0]["free_mb"] == 15274 and devs[1]["description"] == "Intel(R) UHD Graphics"


# ---- paths (SDD 13) -------------------------------------------------------------------------

def test_safe_join_rejects_escapes(tmp_path):
    (tmp_path / "a").mkdir()
    assert safe_join(tmp_path, "a") == (tmp_path / "a").resolve()
    for bad in ("../x", "a/../../x", "/etc/passwd", "C:/Windows", "\\\\server\\share", "a/\x00b"):
        with pytest.raises(AppError):
            safe_join(tmp_path, bad)


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are Windows-only")
def test_safe_join_rejects_junction_escape(tmp_path):
    import _winapi

    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    try:
        _winapi.CreateJunction(str(outside), str(root / "link"))
    except OSError:
        pytest.skip("cannot create junctions here")
    with pytest.raises(AppError):
        safe_join(root, "link/secret.txt")


def test_validate_workspace_root(tmp_path):
    data = tmp_path / "appdata"
    data.mkdir()
    ok, warnings = validate_workspace_root(str(tmp_path), data)
    assert ok == tmp_path.resolve() and warnings == []
    for bad in ("", "relative/path", str(tmp_path / "missing"), "\\\\server\\share", str(data)):
        with pytest.raises(AppError) as e:
            validate_workspace_root(bad, data)
        assert e.value.code == "WORKSPACE_PATH_INVALID"
    anchor = Path(tmp_path.anchor)
    with pytest.raises(AppError):
        validate_workspace_root(str(anchor), data)
    if sys.platform == "win32":
        with pytest.raises(AppError):
            validate_workspace_root(os.environ.get("SystemRoot", "C:\\Windows"), data)


def test_is_within_is_case_insensitive_on_windows(tmp_path):
    assert is_within(tmp_path / "x" / "y", tmp_path)
    assert not is_within(tmp_path.parent, tmp_path)
    assert not is_within(str(tmp_path) + "_sibling", tmp_path)
    if sys.platform == "win32":
        assert is_within(str(tmp_path / "x").upper(), tmp_path)


@pytest.mark.parametrize("pattern, path, hit", [
    ("node_modules/**", "node_modules", True),
    ("node_modules/**", "web/node_modules/react/index.js", True),
    ("node_modules/**", "my_node_modules.txt", False),
    ("*.tmp", "a/b/c.tmp", True),
    ("*.tmp", "a/b/c.tmp.pdf", False),
    ("~$*", "Docs/~$report.docx", True),
    ("Backups/**", "Backups/2024/x.pdf", True),
    ("Backups/**", "Old/Backups/x.pdf", True),
    ("/Backups/**", "Old/Backups/x.pdf", False),
    ("**/cache/*.bin", "a/b/cache/x.bin", True),
])
def test_exclusion_globs(pattern, path, hit):
    assert ExclusionMatcher([pattern]).matches(path) is hit


def test_short_summary_cuts_at_word_boundary():
    assert short_summary(None) is None and short_summary("") is None
    assert short_summary("  Rental   agreement\nfor a flat. ") == "Rental agreement for a flat."
    long = "Mietvertrag " * 30
    out = short_summary(long)
    assert len(out) <= 160 and out.endswith("…") and out[:-1].endswith("Mietvertrag")
    assert len(short_summary("x" * 400)) == 160  # no space to cut at: hard cut
