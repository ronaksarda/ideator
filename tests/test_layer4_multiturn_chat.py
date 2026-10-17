"""Turn 2+ behaviour of POST /api/chat: graph-only refinement, versioning, re-scoring."""
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from ideator.backend import main
from ideator.backend.session import classify_intent


@pytest.fixture
def client():
    return TestClient(main.app)


def chat(client, sid, message):
    resp = client.post("/api/chat", json={"session_id": sid, "message": message})
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]
    events = [json.loads(c[6:]) for c in resp.text.split("\n\n") if c.startswith("data: ")]
    assert not [e for e in events if e["type"] == "error"], events
    assert events[-1]["type"] == "done"
    first = {}
    for e in events:  # first of each type: the version graph_delta precedes the agent's decision delta
        first.setdefault(e["type"], e)
    return first


@pytest.fixture
def session(client, monkeypatch):
    calls = []
    real = main.search_market

    def counting_search(idea):
        calls.append(idea)
        return real(idea)

    monkeypatch.setattr(main, "search_market", counting_search)
    sid = f"test-{uuid.uuid4()}"
    first = chat(client, sid, "Cloud logging and observability tool")
    return sid, first, calls


def test_turn1_scrapes_once_and_creates_v1(session):
    _, first, calls = session
    assert first["intent"]["intent"] == "new_concept"
    assert len(calls) == 1
    idea_nodes = [n for n in first["graph_delta"]["nodes"] if n["data"]["type"] == "idea"]
    assert [n["data"]["version"] for n in idea_nodes] == [1]
    assert any(n["data"]["type"] == "grievance" for n in first["graph_delta"]["nodes"])


def test_turn2_refines_without_rescraping(client, session):
    sid, first, calls = session
    second = chat(client, sid, "Let's make it local-only using SQLite, stripping all cloud egress")

    assert len(calls) == 1, "turn 2 must not hit the search API"
    assert second["intent"]["intent"] == "refine"
    assert second["intent"]["scraped"] is False

    delta = second["graph_delta"]
    assert [n["data"]["version"] for n in delta["nodes"]] == [2]
    labels = [e["data"]["label"] for e in delta["edges"]]
    assert "EVOLVED_FROM" in labels
    assert labels.count("SOLVES") >= 1
    assert "OMITS" in labels

    # SOLVES edges point at grievance nodes that already existed after turn 1.
    turn1_ids = {n["data"]["id"] for n in first["graph_delta"]["nodes"]}
    solved = [e["data"]["target"] for e in delta["edges"] if e["data"]["label"] == "SOLVES"]
    assert set(solved) <= turn1_ids

    # Delta only: the market nodes are not re-sent.
    assert len(delta["nodes"]) == 1

    assert second["score"]["previous"] == first["score"]["saturation_index"]
    assert second["score"]["saturation_index"] < first["score"]["saturation_index"]
    assert any(c["type"] == "cypher" for c in [second["cypher"]])
    assert any("EVOLVED_FROM" in q for q in second["cypher"]["statements"])


def test_question_reads_graph_without_new_version(client, session):
    sid, _, calls = session
    chat(client, sid, "Let's make it local-only using SQLite, stripping all cloud egress")
    q = chat(client, sid, "What pricing should I pick?")
    assert q["intent"]["intent"] == "question"
    assert "graph_delta" not in q
    assert len(calls) == 1
    assert [v["version"] for v in q["score"]["lineage"]] == [1, 2]
    assert "$" in q["message"]["text"]


def test_revert_branches_from_parent_and_supersedes_head(client, session):
    sid, _, _ = session
    chat(client, sid, "What if I add zero-egress P2P sync?")
    third = chat(client, sid, "Actually drop that, let's target solo devs and cut multi-tenant billing instead")
    edges = {(e["data"]["label"], e["data"]["target"]) for e in third["graph_delta"]["edges"]}
    lineage = {v["version"]: v for v in third["score"]["lineage"]}
    assert ("EVOLVED_FROM", lineage[1].get("node", lineage[1]["id"])) in edges  # v1 is drawn under its graph node id
    assert ("SUPERSEDES", lineage[2]["id"]) in edges
    assert lineage[2]["superseded"] and lineage[3]["active"]
    # v3 does not inherit v2's SOLVES edges.
    assert third["score"]["solved"] == sum(1 for e in third["graph_delta"]["edges"] if e["data"]["label"] == "SOLVES")


def test_state_survives_without_client_history(client, session):
    """Session state is rebuilt from the graph; the client sends no chat history."""
    sid, _, _ = session
    chat(client, sid, "Let's make it local-only using SQLite, stripping all cloud egress")
    state = client.get(f"/api/session/{sid}").json()
    assert state["exists"]
    assert [v["version"] for v in state["lineage"]] == [1, 2]
    ids = {n["data"]["id"] for n in state["nodes"]}
    assert all(e["data"]["source"] in ids for e in state["edges"] if e["data"]["label"] in ("SOLVES", "OMITS"))
    assert client.get(f"/api/session/missing-{uuid.uuid4()}").json() == {"exists": False}


def test_unrelated_refinement_links_nothing_and_says_so(client, session):
    sid, _, _ = session
    r = chat(client, sid, "Let's add a mascot named Gerald")
    assert [e["data"]["label"] for e in r["graph_delta"]["edges"]] == ["EVOLVED_FROM"]
    assert "new idea:" in r["message"]["text"]


def test_new_idea_prefix_starts_fresh_root(client, session):
    sid, _, calls = session
    r = chat(client, sid, "new idea: invoice OCR for freelancers")
    assert r["intent"]["intent"] == "new_concept"
    assert calls[-1] == "invoice OCR for freelancers"
    assert [v["version"] for v in r["score"]["lineage"]] == [1]


@pytest.mark.parametrize("msg,kind", [
    ("How do I beat them on that?", "question"),
    ("What if I add zero-egress P2P sync?", "refine"),
    ("Cut the dashboards", "refine"),
    ("new idea: something else", "new_concept"),
])
def test_intent_router(msg, kind):
    assert classify_intent(msg, has_lineage=True)["kind"] == kind


def test_chat_rejects_empty_message(client):
    assert client.post("/api/chat", json={"session_id": "x", "message": ""}).status_code == 422


def test_guest_mode_when_oauth_unset(client, monkeypatch):
    monkeypatch.setattr(main.settings, "github_client_id", None)
    assert client.get("/auth/me").json() == {"auth_enabled": False, "user": main_guest()}


def main_guest():
    from ideator.backend.auth import GUEST
    return GUEST


def test_auth_required_when_oauth_set(client, monkeypatch):
    monkeypatch.setattr(main.settings, "github_client_id", "id")
    monkeypatch.setattr(main.settings, "github_client_secret", "secret")
    assert client.post("/api/chat", json={"session_id": "abc", "message": "hi"}).status_code == 401
    assert client.get("/api/session/abc").status_code == 401
    r = client.get("/auth/login", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"].startswith("https://github.com/login/oauth/authorize")
    assert client.get("/auth/callback?code=x&state=forged").status_code == 400
    assert client.post("/auth/logout").json() == {"ok": True}


def test_session_id_rejects_injection(client):
    r = client.post("/api/chat", json={"session_id": "a:b' OR 1=1", "message": "hi"})
    assert r.status_code == 422
