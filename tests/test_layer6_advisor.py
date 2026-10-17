"""Structured report rendering, node insights, and transcript download."""
import json
import uuid

from fastapi.testclient import TestClient

from ideator.backend import main
from ideator.backend.agent import node_insights, render_report_md

REPORT = {
    "idea": "Local-first notes app",
    "meaning": "For privacy-minded writers.",
    "improvements": ["Ship offline sync first"],
    "competitors": [{"name": "Notion", "best_at": "Databases", "worst_at": "Offline", "users_say": "Too bloated", "price": "$10/mo"}],
    "user_needs": ["Fast capture"],
    "pricing": {"market_range": "$0-$20", "users_will_pay": "$5", "recommended": "$6/mo", "why": "Under Notion", "feature_to_justify_price": "E2E sync"},
    "features": [{"name": "Offline Mode", "verdict": "build", "why": "Top complaint"}],
    "build_plan": [{"step": "MVP", "detail": "Markdown editor"}],
}


def test_render_report_has_every_section_in_order():
    md = render_report_md(REPORT)
    heads = ["The idea", "What it means", "How to improve it", "Competitors", "What users actually need", "Pricing", "How to build it"]
    positions = [md.index(f"## {h}") for h in heads]
    assert positions == sorted(positions)
    assert "| Notion | Databases | Offline | Too bloated | $10/mo |" in md


def test_node_insights_map_by_type_and_label():
    nodes = [{"data": {"id": i, "type": t, "label": l}} for i, t, l in [
        ("c", "competitor", "Notion"), ("f", "feature", "offline mode"), ("p", "pricing", "$10/mo"), ("g", "grievance", "x")]]
    ins = node_insights(REPORT, nodes)
    assert ins["c"]["Worst at"] == "Offline"
    assert ins["f"]["Verdict"] == "build"
    assert ins["p"]["Your price"] == "$6/mo"
    assert "g" not in ins


def test_transcript_download_contains_both_sides():
    client = TestClient(main.app)
    sid = f"test-{uuid.uuid4()}"
    client.post("/api/chat", json={"session_id": sid, "message": "Cloud logging and observability tool"})
    client.post("/api/chat", json={"session_id": sid, "message": "Undercut them at $9 a month"})
    resp = client.get(f"/api/session/{sid}/transcript")
    assert resp.status_code == 200
    assert "attachment" in resp.headers["content-disposition"]
    assert "Cloud logging and observability tool" in resp.text and "Undercut them at $9 a month" in resp.text
    assert resp.text.count("### Ideator") == 2


def test_chat_refuses_scripts_profanity_and_too_short_ideas():
    client = TestClient(main.app)
    for msg in ["<script>alert(1)</script> app", "a shit app for kids", "app"]:
        resp = client.post("/api/chat", json={"session_id": f"test-{uuid.uuid4()}", "message": msg})
        assert resp.status_code == 400 and resp.json()["detail"]


def test_rate_limiter_blocks_fourth_idea_in_window():
    from ideator.backend.guard import RateLimiter
    rl = RateLimiter(3, 3600)
    assert [rl.retry_after("u", t) for t in (0, 1, 2)] == [0, 0, 0]
    assert rl.retry_after("u", 3) > 0 and rl.retry_after("other", 3) == 0
    assert rl.retry_after("u", 3601) == 0


def test_build_brief_converges_over_turns():
    from ideator.backend.agent import first_brief, merge_brief, next_question
    brief, q = first_brief({"who": "students", "pain": "plans ignore budget", "edge": "cheaper", "price": "$4/mo", "mvp": "list"})
    assert brief == {"who": "", "pain": "plans ignore budget", "edge": "", "price": "$4/mo", "mvp": ""}  # founder slots stay open
    assert q.startswith("Who pays first?") and "students" in q
    brief = merge_brief(brief, {"who": "students", "pain": ""})  # an empty answer never erases a decided slot
    assert brief["pain"] == "plans ignore budget" and brief["who"] == "students"
    assert next_question({k: "x" for k in brief}) == ""


def test_agent_spec_carries_decisions_scope_and_evidence():
    from ideator.backend.agent import agent_spec_md
    mem = {"report": {"idea": "Shared grocery list", "competitors": [{"name": "Mealime", "price": "$0", "worst_at": "sharing"}],
                      "features": [{"name": "Recipe feed", "verdict": "cut", "why": "everyone has one"}],
                      "sources": [{"quote": "lists are messy", "about": "Mealime", "url": "https://reddit.com/x"}]},
           "brief": {"who": "roommates", "pain": "", "edge": "", "price": "$4/mo", "mvp": ""}, "brief_question": "Who pays first?"}
    md = agent_spec_md(mem, [{"feature": "Cost split", "ideas": ["split button"], "problem": "awkward math"}], ["Lead with cost split"])
    assert md.startswith("# AGENTS.md: Shared grocery list")
    assert "**Who pays:** roommates" in md and "_not decided yet_" in md
    assert "1. **Cost split**: split button" in md and "Recipe feed: everyone has one" in md
    assert "Lead with cost split" in md and "https://reddit.com/x" in md and "Who pays first?" in md
