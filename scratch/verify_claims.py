"""End-to-end check of every feature the README claims, against the running app (http://127.0.0.1:8000),
FalkorDB and Temporal. Usage: python scratch/verify_claims.py"""
import json
import subprocess
import sys
import time
import uuid

import requests

sys.path.insert(0, ".")
from ideator.backend.graph import MarketGraph, entity_id  # noqa: E402

BASE = "http://127.0.0.1:8000"
results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}", flush=True)


def chat(s, sid, msg):
    t = time.time()
    r = s.post(f"{BASE}/api/chat", json={"session_id": sid, "message": msg}, stream=True, timeout=600)
    if r.status_code != 200:
        return r.status_code, [], time.time() - t, r.text
    evs = [json.loads(line[6:]) for line in r.iter_lines(decode_unicode=True) if line and line.startswith("data: ")]
    return 200, evs, time.time() - t, ""


def first(evs, kind):
    return next((e for e in evs if e.get("type") == kind), None)


s = requests.Session()
sid = f"verify-{uuid.uuid4().hex[:8]}"

# ---- turn 1: new idea ----
code, evs, secs, _ = chat(s, sid, "Meal kit planner for college students on a budget")
check("new idea streams to done", code == 200 and evs and evs[-1]["type"] == "done", f"{secs:.0f}s")
check("no error event", not first(evs, "error"), (first(evs, "error") or {}).get("text", ""))
msg = next((e for e in reversed(evs) if e["type"] == "message"), {})
check("live web search used", msg.get("source_mode") == "LIVE_SERPAPI", msg.get("source_mode"))
tools = [e["tool"] for e in evs if e["type"] == "tool_call"]
check("agent called MCP tools", len(tools) >= 3, " > ".join(tools))
check("agent recorded a decision", "record_decision" in tools)
check("TabPFN predict_price called", "predict_price" in tools)
pp = next((e for e in evs if e.get("tool") == "predict_price"), None)
check("TabPFN returned a price", bool(pp and pp.get("rows") and "predicted_price_usd" in pp["rows"][0]), json.dumps((pp or {}).get("rows") or (pp or {}).get("error")))
check("tool steps carry real Cypher", all(e.get("cypher") or e.get("error") for e in evs if e["type"] == "tool_call"))
text = msg.get("text", "")
for h in ["The idea", "What it means", "How to improve it", "Competitors", "What users actually need", "Pricing"]:
    check(f"report section '{h}'", f"## {h}" in text)
check("report cites sources", "[source](http" in text)
check("report ends with build brief", "## Your build brief" in text)
check("plan event carries the brief", bool((first(evs, "plan") or {}).get("brief")))
plan = first(evs, "plan")
check("action plan sent", bool(plan and plan["items"]), f"{len((plan or {}).get('items', []))} items")
check("plan items have concrete ideas", bool(plan and any(p.get("ideas") for p in plan["items"])))
gd = first(evs, "graph_delta")
types = {n["data"]["type"] for n in (gd or {}).get("nodes", [])}
check("graph has idea/company/feature/comment", {"idea", "competitor", "feature", "grievance"} <= types, str(sorted(types)))
labels = {e["data"]["label"] for e in (gd or {}).get("edges", [])}
check("graph edges idea->company->feature", {"TARGETS", "OFFERS"} <= labels, str(sorted(labels)))

# ---- FalkorDB state ----
g = MarketGraph()
gg = g._falkor.select_graph(f"user_{entity_id('usr', sid)}")
q = lambda c: gg.query(c).result_set  # noqa: E731
check("FalkorDB: idea + competitors stored", q("MATCH (:UserIdea)-[:TARGETS]->(c:Competitor) RETURN count(c)")[0][0] > 0)
check("FalkorDB: decision with BECAUSE evidence", q("MATCH (d:Decision)-[:BECAUSE]->() RETURN count(*)")[0][0] > 0)
check("FalkorDB: complaints stored with source url", q("MATCH (g:Grievance) WHERE g.url STARTS WITH 'http' RETURN count(g)")[0][0] > 0)
hist = g._falkor.select_graph("market_history").query("MATCH (s:Sample) RETURN count(s)").result_set[0][0]
check("FalkorDB: market_history grows for TabPFN", hist > 0, f"{hist} samples")

# ---- turn 2: refine ----
code, evs2, secs2, _ = chat(s, sid, "Add shared grocery lists for roommates and a $3 semester plan")
check("refine streams to done", code == 200 and evs2 and evs2[-1]["type"] == "done", f"{secs2:.0f}s")
d2 = first(evs2, "graph_delta") or {}
vnode = (d2.get("nodes") or [{}])[0].get("data", {})
evo = [e for e in d2.get("edges", []) if e["data"]["label"] == "EVOLVED_FROM"]
known = {n["data"]["id"] for n in gd["nodes"]} if gd else set()
check("v2 linked to a drawn v1 node", bool(evo) and evo[0]["data"]["target"] in known)
check("FalkorDB: v2 stored with EVOLVED_FROM", q("MATCH (:UserIdea)-[:EVOLVED_FROM]->(:UserIdea) RETURN count(*)")[0][0] > 0)
reply = next((e for e in reversed(evs2) if e["type"] == "message"), {}).get("text", "")
check("follow-up has 'What to do next'", "What to do next" in reply)
check("plan refreshed after refine", bool(first(evs2, "plan")))
import re as _re
_n = lambda t: int((_re.search(r"build brief \((\d)/5", t) or [0, -1])[1])
check("brief carries over and does not shrink", _n(reply) >= _n(text) >= 0, f"{_n(text)}/5 -> {_n(reply)}/5")

# ---- turn 3: question is read-only ----
before = q("MATCH (d:Decision) RETURN count(d)")[0][0]
code, evs3, secs3, _ = chat(s, sid, "How do I beat the cheapest competitor?")
check("question answered", code == 200 and any(e["type"] == "message" for e in evs3), f"{secs3:.0f}s")
check("question wrote no decision", q("MATCH (d:Decision) RETURN count(d)")[0][0] == before)

# ---- sessions, transcript, ownership, safety ----
lst = s.get(f"{BASE}/api/sessions").json()["sessions"]
drawn = {n["data"]["id"] for n in s.get(f"{BASE}/api/session/{sid}").json().get("nodes", [])}
lost = [r[0] for r in q("MATCH (g:Grievance) RETURN g.id") if r[0] not in drawn]
check("every stored user comment is on the map", not lost, f"{len(lost)} missing")
import os as _os
check("session key survives restarts", bool(_os.getenv("SESSION_SECRET")) or _os.path.exists("ideator/data/.session_secret"))
check("past sessions lists this session", any(x["session_id"] == sid for x in lst))
tr = s.get(f"{BASE}/api/session/{sid}/transcript")
check("transcript downloads", tr.status_code == 200 and "attachment" in tr.headers.get("content-disposition", ""))
other = requests.Session()
check("other visitor cannot read session", other.get(f"{BASE}/api/session/{sid}").json() == {"exists": False})
check("other visitor cannot write session", other.post(f"{BASE}/api/chat", json={"session_id": sid, "message": "steal this idea please"}).status_code == 403)
check("path traversal id refused", s.get(f"{BASE}/api/session/..%5Cprofiles").status_code in (400, 404))
for bad in ["<script>alert(1)</script> my app", "this shit app idea", "app"]:
    check(f"filter refuses {bad[:18]!r}", s.post(f"{BASE}/api/chat", json={"session_id": f"v-{uuid.uuid4().hex[:6]}", "message": bad}).status_code == 400)
health = s.get(f"{BASE}/health").json()
check("health hides DB host", "falkordb_host" not in health and health.get("falkordb_live") is True)

# ---- voice ----
tts = s.post(f"{BASE}/api/tts", json={"text": "Hello from Ideator"})
check("ElevenLabs voice returns audio", tts.status_code == 200 and tts.headers.get("content-type", "").startswith("audio"), f"{len(tts.content)} bytes")

# ---- Temporal ----
out = subprocess.run(["docker", "exec", "temporal-ideator", "temporal", "workflow", "list", "--address", "localhost:7233", "--limit", "10"],
                     capture_output=True, text=True).stdout
check("Temporal ran SearchWorkflow", "SearchWorkflow" in out and "Completed" in out)
check("Temporal ran ReportWorkflow", "ReportWorkflow" in out)

print(f"\n{sum(ok for _, ok, _ in results)}/{len(results)} passed")
