"""Full pipeline on the fixture workspace (SDD 15.1): index -> search -> summaries -> chat -> purge -> re-index.

Uses the hash test embedder and the stub LLM server, so it runs offline in ~1 minute.
"""

import re
import time

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import auth, make_config, parse_sse, wait_for
from tests.fixtures.make_fixtures import build_fixture_folder
from tests.search_eval import evaluate
from tests.stub_llm import RAG_ANSWER, REFUSAL_ANSWER, StubLLMServer


def _idle_and_summarised(c) -> bool:
    s = c.get("/api/v1/index/status").json()
    return s["state"] == "idle" and s["pending"] == 0 and s["processing"] == 0 and s["summaries_pending"] == 0


EVENTS: list[tuple[str, str, dict]] = []


def _record_events(c) -> None:
    """Keep a copy of every event the backend publishes (the SSE streams read the same bus)."""
    bus = c.app.state.ctx.bus
    publish = bus.publish

    def recording(topic, event, data):
        EVENTS.append((topic, event, dict(data)))
        publish(topic, event, data)

    bus.publish = recording


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    base = tmp_path_factory.mktemp("pipeline")
    ws = build_fixture_folder(base / "workspace")
    with StubLLMServer() as llm:
        with TestClient(create_app(make_config(base / "app", llm_url=llm.url))) as c:
            auth(c)
            _record_events(c)
            assert c.post("/api/v1/models/emb-test/activate").status_code == 200
            assert c.post("/api/v1/models/llm-test/activate").status_code == 200
            assert c.put("/api/v1/workspace", json={"root_path": str(ws)}).status_code == 200
            assert c.post("/api/v1/index/start").status_code == 202
            assert wait_for(lambda: _idle_and_summarised(c), timeout=240), c.get("/api/v1/index/status").json()
            yield c, ws, llm


def _paths(c, query, **extra):
    r = c.post("/api/v1/search", json={"query": query, **extra})
    assert r.status_code == 200, r.text
    return [x["rel_path"] for x in r.json()["results"]]


def test_statuses_and_exclusions(env):
    c, ws, _ = env
    s = c.get("/api/v1/index/status").json()
    assert (s["total"], s["indexed"], s["skipped"], s["errors"]) == (17, 15, 2, 0)
    assert s["summaries_done"] == 15
    files = {f["rel_path"]: f for f in c.get("/api/v1/files?limit=100").json()["items"]}
    assert files["Misc/empty.txt"]["skip_reason"] == "empty"
    assert files["Misc/song.mp3"]["skip_reason"] == "unsupported"
    for excluded in ("node_modules/pkg/readme.txt", ".hidden/secret.txt", "Misc/~$lockfile.docx"):
        assert excluded not in files


def test_file_processing_event_before_each_extraction(env):
    c, _, _ = env
    # unsupported types (song.mp3) are skipped at scan time and never extracted, so they get no event
    files = {f["id"]: f["rel_path"] for f in c.get("/api/v1/files?limit=100").json()["items"]
             if f["skip_reason"] != "unsupported"}
    index = [(e, d) for t, e, d in EVENTS if t == "index"]
    started = {d["file_id"]: d["rel_path"] for e, d in index if e == "file_processing"}
    assert started == files  # every extracted file (indexed or skipped) was announced, with its path
    for fid in started:
        first = next(i for i, (e, d) in enumerate(index) if e == "file_processing" and d["file_id"] == fid)
        done = next(i for i, (e, d) in enumerate(index) if e in ("file_indexed", "file_skipped") and d["file_id"] == fid)
        assert first < done


def test_file_list_has_tags_and_short_summary(env):
    c, _, _ = env
    items = c.get("/api/v1/files?limit=100").json()["items"]
    by_path = {f["rel_path"]: f for f in items}
    for f in items:
        assert isinstance(f["tags"], list) and "summary_short" in f
        if f["status"] == "indexed":
            detail = c.get(f"/api/v1/files/{f['id']}").json()
            assert f["tags"] == [t["name"] for t in detail["tags"]]
            assert f["summary_short"] and len(f["summary_short"]) <= 160
            full = " ".join(detail["summary"]["text"].split())
            assert full.startswith(f["summary_short"].rstrip("…"))
        else:
            assert f["tags"] == [] and f["summary_short"] is None
    assert by_path["Housing/Mietvertrag_2024.pdf"]["tags"]


@pytest.mark.parametrize("query, expected", [
    ("Mietvertrag Kaution", "Housing/Mietvertrag_2024.pdf"),
    ("INV-20417", "Finance/Invoice_Laptop_2024.pdf"),
    ("Haftpflichtversicherung", "Scans/Scanned_Insurance_Letter.pdf"),   # OCR'd scanned PDF
    ("cappuccino", "Images/receipt_coffee.png"),                          # OCR'd image
    ("overfitting", "University/Machine_Learning_Intro.pptx"),
    ("Lebenslauf Python", "Personal/Lebenslauf_Anna_Schmidt.docx"),
    ("pancakes", "Notes/Pancake_Recipe.md"),
])
def test_search_expected_file_in_top3(env, query, expected):
    c, _, _ = env
    assert expected in _paths(c, query)[:3]


def test_search_eval_with_hash_embedder(env):
    c, _, _ = env
    res = evaluate(lambda q: _paths(c, q, limit=10))
    # the hash embedder has no cross-lingual semantics; the real model reaches recall@10 = 1.0
    assert res["recall@10"] >= 0.7, res


def test_search_filters_and_quick_mode(env):
    c, _, _ = env
    assert _paths(c, "Berlin", filters={"folder": "Finance"}) and all(
        p.startswith("Finance/") for p in _paths(c, "Berlin", filters={"folder": "Finance"}))
    assert all(p.endswith(".pdf") for p in _paths(c, "Anna", filters={"ext": ["pdf"]}))
    assert _paths(c, "Miete", filters={"doc_type": "contract"}) == ["Housing/Mietvertrag_2024.pdf"]
    quick = c.post("/api/v1/search", json={"query": "rent", "mode": "quick"}).json()["results"]
    assert len(quick) <= 6 and "summary" not in quick[0]
    full = c.post("/api/v1/search", json={"query": "Mietvertrag"}).json()["results"][0]
    assert full["summary"].startswith("Stub summary") and full["doc_type"] == "contract" and full["tags"]
    assert "<mark>" in full["snippet"]["text"]


def test_tree_detail_chunks_preview(env):
    c, _, _ = env
    root = {n["name"]: n for n in c.get("/api/v1/files/tree").json()}
    assert root["Housing"]["type"] == "dir" and root["Housing"]["child_count"] == 1
    leaf = c.get("/api/v1/files/tree", params={"parent": "Housing"}).json()[0]
    d = c.get(f"/api/v1/files/{leaf['file_id']}").json()
    assert d["language"] == "de" and d["summary"]["doc_type"] == "contract" and d["chunk_count"] >= 1
    chunks = c.get(f"/api/v1/files/{leaf['file_id']}/chunks").json()
    assert chunks["total"] >= 2 and any(ch["source"] == "summary" for ch in chunks["items"])
    img = next(f for f in c.get("/api/v1/files?kind=image").json()["items"])
    r = c.get(f"/api/v1/files/{img['id']}/preview")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and r.content[:2] == b"\xff\xd8"


def test_user_tags(env):
    c, _, _ = env
    fid = c.get("/api/v1/files?q_name=Steuer").json()["items"][0]["id"]
    d = c.put(f"/api/v1/files/{fid}/tags", json={"tags": ["Steuern", "2023", "steuern"]}).json()
    user = sorted(t["name"] for t in d["tags"] if t["source"] == "user")
    assert user == ["2023", "steuern"]
    assert _paths(c, "Euro", filters={"tags": ["2023"]}) == ["Finance/Steuererklaerung_2023.pdf"]


def test_chat_rag_with_citations_and_rewrite(env):
    c, _, llm = env
    sid = c.post("/api/v1/chat/sessions", json={}).json()["id"]
    with c.stream("POST", f"/api/v1/chat/sessions/{sid}/messages", json={"content": "Mietvertrag: wie hoch ist die Miete?"}) as r:
        events = parse_sse(r.iter_lines())
    names = [e for e, _ in events]
    assert names[0] == "meta" and names[1] == "files" and names[-2:] == ["sources", "done"]
    assert "".join(d["t"] for e, d in events if e == "token") == RAG_ANSWER
    cites = dict(events)["sources"]["citations"]
    assert cites and cites[0]["n"] == 1 and cites[0]["file_id"] > 0
    done = dict(events)["done"]
    assert done["tokens_in"] == 123 and done["failed"] is False
    # the system prompt carries numbered, quoted passages
    system = next(m for m in llm.app.state.requests[-1]["messages"] if m["role"] == "system")["content"]
    assert "[1] (file:" in system and '"""' in system
    # one numbered source per file, labelled with its doc type and short summary
    names = [n.strip() for n in re.findall(r"^\[\d+\] \(file: ([^·)]+)", system, re.M)]
    assert names and len(names) == len(set(names))
    assert '(file: Mietvertrag_2024.pdf · contract · "Stub summary of Mietvertrag_2024.pdf.")' in system
    # second turn -> query rewrite request (no system prompt) happens first
    n_before = len(llm.app.state.requests)
    with c.stream("POST", f"/api/v1/chat/sessions/{sid}/messages", json={"content": "und die Kaution?"}) as r:
        list(r.iter_lines())
    new = llm.app.state.requests[n_before:]
    assert "Last question: und die Kaution?" in new[0]["messages"][0]["content"]
    session = c.get(f"/api/v1/chat/sessions/{sid}").json()
    assert session["title"].startswith("Mietvertrag") and len(session["messages"]) == 4
    assert session["messages"][1]["citations"][0]["n"] == 1


def test_watcher_picks_up_new_changed_and_deleted_files(env):
    c, ws, _ = env
    f = ws / "Notes" / "gym.txt"
    f.write_text("Gym membership: monthly fee 39 EUR.", encoding="utf-8")
    assert wait_for(lambda: "Notes/gym.txt" in _paths(c, "gym membership"), timeout=30)
    fid = c.get("/api/v1/files?q_name=gym").json()["items"][0]["id"]
    f.rename(ws / "Notes" / "fitness.txt")
    assert wait_for(lambda: c.get(f"/api/v1/files/{fid}").json().get("rel_path") == "Notes/fitness.txt", timeout=30)
    (ws / "Notes" / "fitness.txt").unlink()
    assert wait_for(lambda: c.get(f"/api/v1/files/{fid}").status_code == 404, timeout=30)


def test_reindex_file_and_remove_from_index(env):
    c, _, _ = env
    fid = c.get("/api/v1/files?q_name=Pancake").json()["items"][0]["id"]
    assert c.post(f"/api/v1/files/{fid}/reindex").status_code == 202
    assert wait_for(lambda: c.get(f"/api/v1/files/{fid}").json()["status"] == "indexed", timeout=30)
    d = c.delete(f"/api/v1/files/{fid}/index").json()
    assert d["status"] == "skipped" and d["skip_reason"] == "excluded" and d["chunk_count"] == 0
    assert "Notes/Pancake_Recipe.md" not in _paths(c, "pancakes")


def test_embedding_switch_requires_confirmation(env):
    c, _, _ = env
    r = c.post("/api/v1/models/emb-test2/activate", json={})
    assert r.status_code == 409 and r.json()["error"]["code"] == "REINDEX_REQUIRED"
    r = c.post("/api/v1/models/emb-test2/activate", json={"confirm_reindex": True})
    assert r.status_code == 200 and r.json()["reindex"] is True
    time.sleep(1)
    assert wait_for(lambda: _idle_and_summarised(c), timeout=120)
    assert c.get("/api/v1/index/status").json()["embedding_model"] == "emb-test2"
    assert "Finance/Invoice_Laptop_2024.pdf" in _paths(c, "INV-20417")[:3]


def test_purge_index_then_reindex(env):
    c, _, _ = env
    r = c.post("/api/v1/privacy/purge", json={"scope": "index", "confirm": True}).json()
    assert r["items_deleted"] > 0
    o = c.get("/api/v1/privacy/overview").json()
    assert (o["chunks"], o["vectors"], o["summaries"], o["files_indexed"]) == (0, 0, 0, 0)
    assert _paths(c, "Mietvertrag") == [] or _paths(c, "Mietvertrag")[0] == "Housing/Mietvertrag_2024.pdf"
    assert c.post("/api/v1/index/start").status_code == 202
    assert wait_for(lambda: c.get("/api/v1/index/status").json()["indexed"] >= 14, timeout=180)
    assert "Housing/Mietvertrag_2024.pdf" in _paths(c, "Mietvertrag Kaution")[:3]


def _ask(c, sid, content):
    with c.stream("POST", f"/api/v1/chat/sessions/{sid}/messages", json={"content": content}) as r:
        events = parse_sse(r.iter_lines())
    return "".join(d["t"] for e, d in events if e == "token"), dict(events)


def _system_requests(llm) -> int:
    return sum(1 for b in llm.app.state.requests if b["messages"] and b["messages"][0]["role"] == "system")


def test_chat_refusal_is_retried_once_when_search_found_the_file(env):
    c, _, llm = env
    sid = c.post("/api/v1/chat/sessions", json={}).json()["id"]
    before = _system_requests(llm)
    text, ev = _ask(c, sid, "REFUSE_ONCE Mietvertrag Kaution Miete")
    assert text == RAG_ANSWER  # the held-back refusal never reached the client
    assert _system_requests(llm) == before + 2
    hinted = llm.app.state.requests[-1]["messages"][0]["content"]
    assert "is the closest match" in hinted
    assert ev["sources"]["citations"] and ev["sources"]["citations"][0]["n"] == 1


def test_chat_refusal_has_no_citations(env):
    c, _, _ = env
    sid = c.post("/api/v1/chat/sessions", json={}).json()["id"]
    text, ev = _ask(c, sid, "REFUSE_ALWAYS Mietvertrag Kaution Miete")
    assert text == REFUSAL_ANSWER
    assert ev["sources"]["citations"] == []  # it says "not found", so it must not cite files
    assert ev["files"]["results"]  # clients show these as "possibly related"
    session = c.get(f"/api/v1/chat/sessions/{sid}").json()
    assert session["messages"][1]["citations"] == []


def test_chat_rewrite_drift_cannot_hide_the_right_file(env):
    c, _, _ = env
    sid = c.post("/api/v1/chat/sessions", json={}).json()["id"]
    _ask(c, sid, "Mietvertrag: wie hoch ist die Miete?")
    _, ev = _ask(c, sid, "DRIFT_TEST pancakes recipe")
    # the rewrite drifted to the rental contract, but the original question is searched as well
    top = [r["rel_path"] for r in ev["files"]["results"]][:2]
    assert "Notes/Pancake_Recipe.md" in top


def test_summary_prompt_names_the_file_language(env):
    _, _, llm = env
    prompts = [b["messages"][-1]["content"] for b in llm.app.state.requests if b.get("response_format")]
    summaries = [p for p in prompts if "summary:" in p]
    # a worked example (no dates or amounts) is sent as an earlier turn, in the file's language
    lease_req = next(b for b in llm.app.state.requests
                     if b.get("response_format") and "File name: Mietvertrag_2024.pdf" in b["messages"][-1]["content"]
                     and "summary:" in b["messages"][-1]["content"])
    assert lease_req["messages"][1]["role"] == "assistant" and "Stadtbibliothek" in lease_req["messages"][1]["content"]
    lease = next(p for p in summaries if "File name: Mietvertrag_2024.pdf" in p)
    invoice = next(p for p in summaries if "File name: Invoice_Laptop_2024.pdf" in p)
    assert "written in German" in lease and "written in English" in invoice
    assert 'not with "This"' in lease and "Use only facts that are written in this document" in lease
    # the doc type comes from its own short classification request
    assert any("decide what kind of document" in p and "File name: Steuererklaerung_2023.pdf" in p for p in prompts)


def test_removed_file_never_comes_back_by_name(env):
    c, _, _ = env
    fid = c.get("/api/v1/files?q_name=Pancake").json()["items"][0]["id"]
    assert c.delete(f"/api/v1/files/{fid}/index").status_code == 200
    assert "Notes/Pancake_Recipe.md" not in _paths(c, "pancake recipe")  # not even through the name bonus
    assert c.post(f"/api/v1/files/{fid}/reindex").status_code == 202  # "Add back"
    assert wait_for(lambda: "Notes/Pancake_Recipe.md" in _paths(c, "pancake recipe"), timeout=60)
    assert wait_for(lambda: _idle_and_summarised(c), timeout=120)


def test_answer_that_names_the_file_still_gets_its_source(env):
    c, _, _ = env
    sid = c.post("/api/v1/chat/sessions", json={}).json()["id"]
    text, ev = _ask(c, sid, "NAME_ONLY Mietvertrag Miete")
    assert "[" not in text
    assert [x["name"] for x in ev["sources"]["citations"]] == ["Mietvertrag_2024.pdf"]


def test_answer_without_marker_is_cited_by_the_numbers_it_quotes(env):
    c, _, _ = env
    sid = c.post("/api/v1/chat/sessions", json={}).json()["id"]
    text, ev = _ask(c, sid, "NUMBERS_ONLY Mietvertrag Miete Kaution")
    assert "[" not in text and "Mietvertrag" not in text
    cites = ev["sources"]["citations"]
    assert [x["name"] for x in cites] == ["Mietvertrag_2024.pdf"]
    # it points at a passage that really holds the numbers, so the client can show and highlight it
    chunks = c.get(f"/api/v1/files/{cites[0]['file_id']}/chunks?limit=50").json()["items"]
    quoted = next(ch["text"] for ch in chunks if ch["id"] == cites[0]["chunk_id"])
    assert "950,00" in quoted and "2.850,00" in quoted


def test_numbers_that_are_in_no_source_cite_nothing(env):
    c, _, _ = env
    sid = c.post("/api/v1/chat/sessions", json={}).json()["id"]
    _, ev = _ask(c, sid, "UNKNOWN_NUMBERS Mietvertrag Miete")
    assert ev["sources"]["citations"] == []


def test_undated_sources_are_labelled(env):
    c, _, llm = env
    sid = c.post("/api/v1/chat/sessions", json={}).json()["id"]
    _ask(c, sid, "coffee receipt cappuccino and the Mietvertrag")
    system = llm.app.state.requests[-1]["messages"][0]["content"]
    receipt = next(line for line in system.splitlines() if "(file: receipt_coffee.png" in line)
    lease = next(line for line in system.splitlines() if "(file: Mietvertrag_2024.pdf" in line)
    assert "· no date" in receipt and "· no date" not in lease  # the receipt has no date, the lease has
