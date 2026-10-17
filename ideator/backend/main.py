import asyncio
import secrets
import json
import statistics
import logging
import re
import os
import time
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

try:
    from backend.config import settings
    from backend.graph import MarketGraph, entity_id, get_user_graph
    from backend.search import search_market
    from backend.tabular import evaluate_market_saturation
    from backend.agent import ResearcherAgent, StrategistAgent, synthesize_attack_blueprint, _gemini_json
    from backend.tools import build_server, run_graph_agent
    from backend.agent import build_idea_report, render_report_md, node_insights, continue_idea_chat, llm_unavailable_note, data_only_report, followup_fallback, merge_brief, brief_md, first_brief, next_question
    from backend.auth import router as auth_router, auth_enabled, require_user, current_user, GUEST
    from backend.session import classify_intent, match_targets, feature_vector
    from backend import guard
    from backend.workflow import ReportWorkflow, SearchWorkflow, durable, start_worker
except ImportError:
    from ideator.backend.config import settings
    from ideator.backend.graph import MarketGraph, entity_id, get_user_graph
    from ideator.backend.search import search_market
    from ideator.backend.tabular import evaluate_market_saturation
    from ideator.backend.agent import ResearcherAgent, StrategistAgent, synthesize_attack_blueprint, _gemini_json
    from ideator.backend.tools import build_server, run_graph_agent
    from ideator.backend.agent import build_idea_report, render_report_md, node_insights, continue_idea_chat, llm_unavailable_note, data_only_report, followup_fallback, merge_brief, brief_md, first_brief, next_question
    from ideator.backend.auth import router as auth_router, auth_enabled, require_user, current_user, GUEST
    from ideator.backend.session import classify_intent, match_targets, feature_vector
    from ideator.backend import guard
    from ideator.backend.workflow import ReportWorkflow, SearchWorkflow, durable, start_worker

if settings.sentry_dsn:
    try:
        import sentry_sdk
        sentry_sdk.init(dsn=settings.sentry_dsn, traces_sample_rate=1.0)
    except Exception:
        pass

app = FastAPI(title="Ideator — Adversarial Market Intelligence Platform")

def _session_secret() -> str:
    """SESSION_SECRET from .env, else a random key generated once and kept in data/ (gitignored) so restarts keep sessions."""
    if settings.session_secret:
        return settings.session_secret
    f = Path(__file__).resolve().parent.parent / "data" / ".session_secret"
    if f.exists() and (key := f.read_text(encoding="utf-8").strip()):
        return key
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(key := secrets.token_hex(32), encoding="utf-8")
    return key


app.add_middleware(SessionMiddleware, secret_key=_session_secret())

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o for o in os.getenv("CORS_ORIGINS", "").split(",") if o],  # same-origin UI needs none
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)


@app.on_event("startup")
async def _start_temporal() -> None:
    if not os.getenv("PYTEST_CURRENT_TEST"):
        await start_worker()

# Global graph singleton for backward compatibility & health checks
graph = MarketGraph()

# Session store: in memory, mirrored to data/ as JSON so ideas and founder notes survive restarts.
SESSION_STORE: Dict[str, Dict[str, Any]] = {}
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
PROFILES_FILE = DATA_DIR / "profiles.json"


def _persist() -> bool:
    return not os.getenv("PYTEST_CURRENT_TEST")


def load_session(session_id: str) -> Optional[Dict[str, Any]]:
    if session_id not in SESSION_STORE and _persist():
        f = DATA_DIR / "sessions" / f"{session_id}.json"
        if f.exists():
            try:
                SESSION_STORE[session_id] = json.loads(f.read_text(encoding="utf-8"))
            except ValueError:
                logging.getLogger("ideator").error("Corrupt session file %s", f)
    return SESSION_STORE.get(session_id)


def save_session(session_id: str) -> None:
    if _persist():
        (DATA_DIR / "sessions").mkdir(parents=True, exist_ok=True)
        f = DATA_DIR / "sessions" / f"{session_id}.json"
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(SESSION_STORE[session_id]), encoding="utf-8")
        os.replace(tmp, f)  # a crash mid-write never leaves half a file


def _profiles() -> Dict[str, str]:
    return json.loads(PROFILES_FILE.read_text(encoding="utf-8")) if _persist() and PROFILES_FILE.exists() else {}


def get_profile(user_key: str) -> str:
    return _profiles().get(user_key, "")


def set_profile(user_key: str, notes: str) -> None:
    if not notes or not _persist():
        return
    profiles = _profiles()
    profiles[user_key] = notes[:2000]
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PROFILES_FILE.write_text(json.dumps(profiles), encoding="utf-8")


SESSION_ID_RE = r"^[a-zA-Z0-9_\-]{1,64}$"  # no dots or slashes: ids become file names


def _owner(request: Request) -> str:
    """Who owns a session: the signed-in user, else a random visitor id kept in the signed session cookie."""
    user = current_user(request)
    if user and user.get("login") != "guest":
        return f"user:{user['login']}"
    if "vid" not in request.session:
        request.session["vid"] = secrets.token_hex(12)
    return f"visitor:{request.session['vid']}"


def _own_session(session_id: str, request: Request) -> Optional[Dict[str, Any]]:
    """The session if this caller may see it, else None (other people's sessions look missing)."""
    if not re.fullmatch(SESSION_ID_RE, session_id):
        raise HTTPException(status_code=400, detail="Invalid session id")
    sess = load_session(session_id)
    if sess and sess.get("owner") not in (None, _owner(request)):
        return None
    return sess


class ChatRequest(BaseModel):
    session_id: str = Field(..., pattern=SESSION_ID_RE)
    message: str = Field(..., min_length=1, max_length=2000)


class AnalyzeRequest(BaseModel):
    idea: str = Field(..., min_length=1, max_length=600)


@app.get("/health")
def health_check():
    ledger = graph.get_memory_ledger()
    source_mode = "LIVE_SERPAPI" if (settings.serpapi_api_key and not settings.mock_serpapi) else "DETERMINISTIC_FALLBACK"
    return {
        "status": "online",
        "falkordb_live": graph.is_live,
        "source_mode": source_mode,
        "memory_ledger": {k: v for k, v in ledger.items() if k != "historical_ideas"}
    }


@app.get("/api/memory")
def get_memory():
    return {k: v for k, v in graph.get_memory_ledger().items() if k != "historical_ideas"}


@app.get("/api/sessions")
def list_sessions(request: Request):
    me = _owner(request)
    sess_dir = DATA_DIR / "sessions"
    file_sessions = []
    if sess_dir.exists():
        for p in sess_dir.glob("*.json"):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                sid = p.stem
                mem = data.get("memory", {})
                first_msg = mem.get("idea") or (mem.get("transcript", [{}])[0].get("text") if mem.get("transcript") else "Market Idea")
                if not mem.get("transcript") or data.get("owner") != me:
                    continue  # only this caller's sessions, and only ones with a conversation
                ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(p.stat().st_mtime))  # last activity, not "now"
                file_sessions.append({
                    "session_id": sid,
                    "title": first_msg[:35] + ("..." if len(first_msg) > 35 else ""),
                    "message_count": len(mem.get("transcript", [])),
                    "timestamp": ts
                })
            except Exception:
                continue
    combined = {s["session_id"]: s for s in file_sessions}  # saved sessions only; graph chat log also holds test runs
    return {"sessions": sorted(combined.values(), key=lambda x: x.get("timestamp", ""), reverse=True)}


@app.get("/api/session/{session_id}")
def get_session(session_id: str, request: Request):
    if auth_enabled():
        require_user(request)
    sess = _own_session(session_id, request)
    if not sess:
        return {"exists": False}
    mem = sess.get("memory", {})
    transcript = mem.get("transcript") or get_user_graph(session_id).get_chat_history(session_id)
    return {
        "exists": True,
        "nodes": sess.get("nodes", []),
        "edges": sess.get("edges", []),
        "score": sess.get("score", {}),
        "lineage": sess.get("lineage", []),
        "transcript": transcript,
        "plan": sess.get("plan", []),
        "brief": mem.get("brief", {}),
        "question": mem.get("brief_question", ""),
        "insights": {n["data"]["id"]: n["data"]["insight"] for n in sess.get("nodes", []) if n["data"].get("insight")}
    }


@app.get("/api/session/{session_id}/transcript")
def download_transcript(session_id: str, request: Request):
    if auth_enabled():
        require_user(request)
    sess = _own_session(session_id, request)
    if not sess:
        raise HTTPException(status_code=404, detail="Unknown session")
    mem = sess.get("memory", {})
    lines = [f"# Ideator transcript\n\n**Idea:** {mem.get('idea', '')}\n"]
    if mem.get("idea_state"):
        lines.append(f"**Where the idea stands now:** {mem['idea_state']}\n")
    if mem.get("brief"):
        lines.append(brief_md(mem["brief"], mem.get("brief_question", "")) + "\n")
    for m in mem.get("transcript", []):
        lines.append(f"---\n\n### {'You' if m['role'] == 'user' else 'Ideator'}\n\n{m['text']}\n")
    return PlainTextResponse("\n".join(lines), media_type="text/markdown",
                             headers={"Content-Disposition": f'attachment; filename="ideator-{session_id[:8]}.md"'})


SESSION_LOCKS: Dict[str, asyncio.Lock] = {}


async def chat_event_stream(session_id: str, message: str, user_key: str = "guest", owner: Optional[str] = None) -> AsyncGenerator[str, None]:
    async with SESSION_LOCKS.setdefault(session_id, asyncio.Lock()):  # two tabs on one session take turns
        try:
            import sentry_sdk  # one Sentry transaction per turn: Gemma calls and MCP tool calls nest under it
            with sentry_sdk.start_transaction(op="gen_ai.invoke_agent", name="invoke_agent ideator"):
                async for ev in _chat_turn(session_id, message.strip(), user_key):
                    yield ev
        except Exception:
            logging.getLogger("ideator").exception("chat turn failed")
            yield f"data: {json.dumps({'type': 'error', 'text': 'Something went wrong on this step. Your earlier work is saved; send the message again.'})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
        finally:
            if session_id in SESSION_STORE:
                SESSION_STORE[session_id].setdefault("owner", owner)
                save_session(session_id)


def _remember(sess: Dict[str, Any], role: str, text: str, session_id: Optional[str] = None) -> None:
    sess.setdefault("memory", {}).setdefault("transcript", []).append({"role": role, "text": text})
    if session_id:
        try:
            get_user_graph(session_id).append_chat_message(session_id, role, text)
        except Exception:
            pass


async def _chat_turn(session_id: str, msg: str, user_key: str) -> AsyncGenerator[str, None]:
    user_graph = get_user_graph(session_id)
    researcher = ResearcherAgent(user_graph)
    strategist = StrategistAgent(user_graph)

    load_session(session_id)
    sess = SESSION_STORE.setdefault(session_id, {
        "lineage": [],
        "version": 0,
        "nodes": [],
        "edges": [],
        "previous_saturation": None,
        "market": {"competitors": {}, "grievances": {}, "features": {}}
    })

    has_lineage = len(sess["lineage"]) > 0
    classified = classify_intent(msg, has_lineage=has_lineage)
    kind = classified.get("kind", "new_concept")

    # If user explicitly starts with "new idea:", force new_concept
    if msg.lower().startswith("new idea:"):
        kind = "new_concept"
        clean_text = msg[9:].strip()
    else:
        clean_text = msg

    if not has_lineage:
        kind = "new_concept"

    # 1. INTENT EVENT
    is_scraped = (kind == "new_concept")
    yield f"data: {json.dumps({'type': 'intent', 'intent': kind, 'scraped': is_scraped})}\n\n"
    await asyncio.sleep(0.05)

    if kind == "question":
        # Question turn: reads graph without new version
        if sess.get("score"):
            yield f"data: {json.dumps(sess['score'])}\n\n"
        async for ev in _graph_agent(sess, user_graph, clean_text, actions=False):  # questions read, never write
            yield ev
        answer = followup_fallback(clean_text, sess["memory"].get("graph_findings") or [], sess.get("plan") or [])
        answer = await _followup(sess, clean_text, user_key, session_id=session_id) or answer
        sess.pop("version_title", None)
        yield _plan_event(sess)
        yield f"data: {json.dumps({'type': 'message', 'text': answer, 'markdown': True})}\n\n"
        yield f"data: {json.dumps({'type': 'done'})}\n\n"
        return

    if kind == "new_concept":
        sess["version"] = 1
        sess["lineage"] = []
        sess["nodes"] = []
        sess["edges"] = []
        sess["memory"] = {"idea": clean_text, "transcript": []}
        _remember(sess, "user", msg, session_id)
        version = 1
        u_id = f"idea_{entity_id('ver', f'{session_id}_{secrets.token_hex(4)}')}"

        yield f"data: {json.dumps({'type': 'agent_step', 'agent': 'ResearcherAgent', 'step': 'Mining market intelligence via SerpApi...'})}\n\n"
        market_data = await durable(SearchWorkflow.run, clean_text, f"search-{u_id}", lambda: search_market(clean_text))
        competitors = market_data.get("competitors", [])
        complaints = market_data.get("complaints", [])

        research_report = await asyncio.to_thread(researcher.research_and_ingest, u_id, clean_text, market_data)
        cypher_stmts = research_report["cypher_queries_executed"]
        yield f"data: {json.dumps({'type': 'cypher', 'statements': cypher_stmts[:6]})}\n\n"

        # Turn 1 graph is read back from FalkorDB (in-memory copy only when the DB is down, e.g. CI)
        idea_node = entity_id("idea", u_id)
        await asyncio.to_thread(user_graph.link_similar_features, idea_node)
        await asyncio.to_thread(user_graph.record_market_samples, idea_node)  # grows TabPFN's pricing data
        cytoscape_elements = await asyncio.to_thread(user_graph.falkor_elements, idea_node) or user_graph.get_full_graph_elements(u_id)
        raw_nodes = cytoscape_elements.get("nodes", [])
        raw_edges = cytoscape_elements.get("edges", [])

        cy_nodes = [
            {
                "data": {
                    **n["data"],
                    "version": 1,
                    "delta": clean_text if n["data"]["id"] == u_id else ""
                }
            }
            for n in raw_nodes
        ]

        cy_edges = [{"data": e["data"]} for e in raw_edges]

        # Populate session market store for multi-turn matching
        sess["market"] = {
            "competitors": {c["name"]: {"name": c["name"], "price": float(c.get("price_usd", 29.0))} for c in competitors},
            "grievances": {entity_id("griev", cmp["text"]): {"id": entity_id("griev", cmp["text"]), "text": cmp["text"], "feature": entity_id("feat", cmp.get("feature", "Core Engine")), "competitor": cmp.get("competitor", "Incumbent")} for cmp in complaints},
            "features": {entity_id("feat", cmp.get("feature", "Core Engine")): {"name": cmp.get("feature", "Core Engine")} for cmp in complaints}
        }

        lineage_item = {
            "version": 1,
            "id": u_id,
            "node": idea_node,  # id of the UserIdea node in FalkorDB and on the canvas
            "delta": clean_text[:40] + ("..." if len(clean_text) > 40 else ""),
            "active": True,
            "superseded": False
        }
        sess["lineage"] = [lineage_item]
        sess["nodes"] = cy_nodes
        sess["edges"] = cy_edges

        yield f"data: {json.dumps({'type': 'graph_delta', 'nodes': cy_nodes, 'edges': cy_edges, 'focus': idea_node, 'superseded': []})}\n\n"

        # Tabular evaluation Turn 1
        first_price = float(statistics.median([float(c.get("price_usd") or 0) for c in competitors])) if competitors else 0.0
        tab_verdict = evaluate_market_saturation(
            competitor_count=len(competitors),
            median_price_usd=first_price,
            complaint_count=len(complaints),
            has_open_source=1,
            review_volume=250
        )
        sat_idx = tab_verdict.get("saturation_index", 75.0)
        rec_price = tab_verdict.get("recommended_entry_price", 19.0)
        sess["previous_saturation"] = sat_idx

        score_payload = {
            "type": "score",
            "version": 1,
            "saturation_index": sat_idx,
            "previous": None,
            "recommended_entry_price": rec_price,
            "solved": 0,
            "omitted": 0,
            "vector": {
                "competitor_count": len(competitors),
                "median_price_usd": first_price,
                "complaint_count": len(complaints),
                "has_open_source": 1,
                "review_volume": 250
            },
            "lineage": sess["lineage"]
        }
        sess["score"] = score_payload
        yield f"data: {json.dumps(score_payload)}\n\n"

        # Strategist Agent Turn 1
        yield f"data: {json.dumps({'type': 'agent_step', 'agent': 'StrategistAgent', 'step': 'Ranking competitors in FalkorDB...'})}\n\n"
        blueprint = await asyncio.to_thread(strategist.strategize, u_id, clean_text, tab_verdict)
        if blueprint.get("reasoning_path"):
            yield f"data: {json.dumps({'type': 'reasoning_path', **blueprint['reasoning_path']})}\n\n"

        ai_engine = blueprint.get("ai_engine_used", "deterministic_synthesis")
        top_vulnerable = (blueprint.get("pagerank_ranking") or [{}])[0].get("competitor", "Incumbents")
        agent_summary = (
            f"[{blueprint['agent']} · Engine: {ai_engine}]\n"
            f"FalkorDB PageRank identified '{top_vulnerable}' as the primary incumbent.\n\n"
            f"Day-1 Wedge: {blueprint['the_wedge']}\n\n"
            f"Day-1 Cuts to Ship Fast:\n"
            + "\n".join([f" • {c}" for c in blueprint.get("mvp_scope_cuts", [])[:3]]) + "\n\n"
            f"Monetization: {blueprint['monetization_structure']}"
        )
        async for ev in _graph_agent(sess, user_graph, "Find the single strongest wedge for this idea using the graph, check it with evidence, estimate its market price with predict_price, then record one decision linked to that evidence."):
            yield ev
        market_data["agent_findings"] = sess["memory"].get("graph_findings", [])
        source_mode = market_data.get("source_mode", "OFFLINE_MOCK")
        yield f"data: {json.dumps({'type': 'agent_step', 'agent': 'Advisor', 'step': f'Writing structured analysis from {source_mode} data...'})}\n\n"
        profile = get_profile(user_key)
        report = await durable(ReportWorkflow.run, {"idea_text": clean_text, "market": market_data, "tab": tab_verdict, "user_profile": profile},
                               f"report-{u_id}", lambda: build_idea_report(clean_text, market_data, tab_verdict, profile))
        note = "" if report else llm_unavailable_note()
        if not report:
            report = data_only_report(clean_text, market_data, tab_verdict)
        if report:
            report["sources"] = [{"quote": c["text"], "about": c.get("competitor", ""), "url": c["source_url"]}
                                 for c in complaints if str(c.get("source_url", "")).startswith("http")][:8]
            agent_summary = render_report_md(report) + note
            sess["memory"]["report"] = report
            sess["memory"]["brief"], q = first_brief(report.get("brief"), str(report.get("next_question") or ""))
            sess["memory"]["brief_question"] = q[:400]
            agent_summary += "\n\n" + brief_md(sess["memory"]["brief"], sess["memory"]["brief_question"])
            sess["plan"] = report.get("action_plan", [])
            yield _plan_event(sess)
            set_profile(user_key, report.get("user_profile", ""))
            insights = node_insights(report, cy_nodes)
            for n in sess["nodes"]:
                if n["data"]["id"] in insights:
                    n["data"]["insight"] = insights[n["data"]["id"]]
            yield f"data: {json.dumps({'type': 'node_insights', 'insights': insights})}\n\n"
        else:
            agent_summary += llm_unavailable_note()
        _remember(sess, "assistant", agent_summary, session_id)
        yield f"data: {json.dumps({'type': 'message', 'text': agent_summary, 'markdown': bool(report), 'source_mode': source_mode, 'reasoning_path': blueprint.get('reasoning_path')})}\n\n"
        yield f"data: {json.dumps({'type': 'done'})}\n\n"
        return

    # Turn 2+ Refinement
    prev_active = [v for v in sess["lineage"] if v.get("active")]
    parent_version = prev_active[-1] if prev_active else sess["lineage"][-1]
    version = len(sess["lineage"]) + 1
    u_id = f"idea_{entity_id('ver', f'{session_id}_{version}')}"


    # What this change addresses, matched against the recorded complaints; nothing matched means unrelated.
    solves_ids, omits_ids = match_targets(clean_text, sess["market"], omit=True)
    if not solves_ids:  # also match complaints whose feature this change names, e.g. "win on Invoice Follow-up"
        low = clean_text.lower()
        named = {fid for fid, f in sess["market"]["features"].items() if len(f["name"]) > 3 and f["name"].lower() in low}
        solves_ids = [gid for gid, g in sess["market"]["grievances"].items() if g["feature"] in named]
    unrelated = not solves_ids and not omits_ids

    # Check for revert / branching
    is_revert = bool(classified.get("revert"))
    superseded_id = None
    if is_revert and len(sess["lineage"]) >= 2:
        superseded_id = sess["lineage"][-1].get("node", sess["lineage"][-1]["id"])
        sess["lineage"][-1]["superseded"] = True
        sess["lineage"][-1]["active"] = False
        target_parent_id = sess["lineage"][0].get("node", sess["lineage"][0]["id"])
    else:
        target_parent_id = parent_version.get("node", parent_version["id"])

    for item in sess["lineage"]:
        item["active"] = False

    new_lineage_item = {
        "version": version,
        "id": u_id,
        "delta": clean_text[:40] + ("..." if len(clean_text) > 40 else ""),
        "active": True,
        "superseded": False
    }
    sess["lineage"].append(new_lineage_item)

    # Delta nodes: only the new version node
    delta_node = {
        "data": {
            "id": u_id,
            "label": f"v{version}: {clean_text[:25]}",
            "type": "idea",
            "version": version,
            "delta": clean_text
        }
    }
    delta_nodes = [delta_node]

    # Delta edges
    delta_edges = [
        {
            "data": {
                "id": f"e_evolve_{target_parent_id}_{u_id}",
                "source": u_id,
                "target": target_parent_id,
                "label": "EVOLVED_FROM"
            }
        }
    ]

    if superseded_id:
        delta_edges.append({
            "data": {
                "id": f"e_super_{u_id}_{superseded_id}",
                "source": u_id,
                "target": superseded_id,
                "label": "SUPERSEDES"
            }
        })

    if not unrelated:
        for gid in solves_ids:
            delta_edges.append({
                "data": {
                    "id": f"e_solves_{u_id}_{gid}",
                    "source": u_id,
                    "target": gid,
                    "label": "SOLVES"
                }
            })
        for fid in omits_ids:
            delta_edges.append({
                "data": {
                    "id": f"e_omits_{u_id}_{fid}",
                    "source": u_id,
                    "target": fid,
                    "label": "OMITS"
                }
            })

    sess["nodes"].extend(delta_nodes)
    sess["edges"].extend(delta_edges)

    root_node = sess["lineage"][0].get("node", entity_id("idea", sess["lineage"][0]["id"]))
    await asyncio.to_thread(user_graph.add_version, root_node, target_parent_id, u_id, clean_text, version,
                            [] if unrelated else solves_ids, [] if unrelated else omits_ids)
    version_cypher = ("MATCH (parent:UserIdea {id: $parent}) MERGE (v:UserIdea {id: $vid}) MERGE (v)-[:EVOLVED_FROM]->(parent) "
                      "WITH v UNWIND $solves AS gid MATCH (g:Grievance {id: gid}) MERGE (v)-[:SOLVES]->(g)")
    yield "data: " + json.dumps({"type": "cypher", "statements": [version_cypher]}) + "\n\n"

    yield f"data: {json.dumps({'type': 'graph_delta', 'nodes': delta_nodes, 'edges': delta_edges, 'focus': u_id, 'superseded': [superseded_id] if superseded_id else []})}\n\n"

    # Tabular score calculation: refinement strictly lowers saturation
    prev_sat = sess.get("previous_saturation") or 75.0
    if unrelated:
        new_sat = prev_sat
    else:
        new_sat = max(25.0, round(prev_sat - (12.5 * len(solves_ids) + 5.0 * len(omits_ids)), 1))
    sess["previous_saturation"] = new_sat

    solved_count = len([e for e in delta_edges if e["data"]["label"] == "SOLVES"])
    omitted_count = len([e for e in delta_edges if e["data"]["label"] == "OMITS"])

    score_payload = {
        "type": "score",
        "version": version,
        "saturation_index": new_sat,
        "previous": prev_sat,
        "recommended_entry_price": (sess.get("score") or {}).get("recommended_entry_price", 0.0),
        "solved": solved_count,
        "omitted": omitted_count,
        "vector": (sess.get("score") or {}).get("vector", {}),
        "lineage": sess["lineage"]
    }
    sess["score"] = score_payload
    yield f"data: {json.dumps(score_payload)}\n\n"

    if unrelated:
        msg_text = "This refinement doesn't address any recorded competitor complaints. If this is a completely different startup, type 'new idea:' to start fresh."
    else:
        msg_text = (
            f"Updated v{version}. Solved {solved_count} complaint(s) and omitted {omitted_count} bloated feature(s). "
            f"Saturation dropped from {prev_sat}% to {new_sat}%."
        )

    async for ev in _graph_agent(sess, user_graph, f"The founder changed the idea: '{clean_text}'. Check this change against the graph and record the decision."):
        yield ev
    msg_text = await _followup(sess, clean_text, user_key, header=msg_text, session_id=session_id) or msg_text
    # Name the new version and let its node explain itself, then refresh the action plan.
    title = sess.pop("version_title", "") or clean_text[:40]
    delta_node["data"].update({"label": f"v{version}: {title}", "insight": {
        "Change": clean_text, "Where the idea stands": sess.get("memory", {}).get("idea_state", "")}})
    new_lineage_item["delta"] = title
    yield f"data: {json.dumps({'type': 'graph_delta', 'nodes': [delta_node], 'edges': [], 'focus': u_id, 'superseded': []})}\n\n"
    yield f"data: {json.dumps(score_payload)}\n\n"  # re-send so the version list shows the new title
    yield _plan_event(sess)
    yield f"data: {json.dumps({'type': 'message', 'text': msg_text, 'markdown': True})}\n\n"
    yield f"data: {json.dumps({'type': 'done'})}\n\n"


def _plan_event(sess: Dict[str, Any]) -> str:
    """Action plan plus the build brief it is converging on, as one SSE event."""
    mem = sess.get("memory", {})
    return f"data: {json.dumps({'type': 'plan', 'items': sess.get('plan', []), 'brief': mem.get('brief', {}), 'question': mem.get('brief_question', '')})}\n\n"


async def _graph_agent(sess: Dict[str, Any], user_graph: MarketGraph, goal: str, actions: bool = True) -> AsyncGenerator[str, None]:
    """Graph agent: Gemma picks MCP graph tools step by step; every step streams with the Cypher it really ran."""
    idea_raw, idea_text = sess["lineage"][0]["id"], sess.get("memory", {}).get("idea", "")
    server = build_server(user_graph, idea_raw, idea_text, search_market, actions=actions)
    q: asyncio.Queue = asyncio.Queue()
    task = asyncio.create_task(run_graph_agent(server, goal, _gemini_json, q.put_nowait))
    yield f"data: {json.dumps({'type': 'agent_step', 'agent': 'GraphAgent', 'step': 'Choosing FalkorDB tools over MCP...'})}\n\n"
    n = 0
    while not (task.done() and q.empty()):
        try:
            s = await asyncio.wait_for(q.get(), 0.3)
        except asyncio.TimeoutError:
            continue
        n += 1
        r = s["result"]
        yield f"data: {json.dumps({'type': 'tool_call', 'step': n, 'tool': s['tool'], 'args': s['args'], 'thought': s['thought'], 'cypher': r.get('cypher', ''), 'ms': r.get('ms'), 'rows': (r.get('rows') or [])[:6], 'node_ids': r.get('node_ids', []), 'error': r.get('error')}, default=str)}\n\n"
    result = task.result()
    sess.setdefault("memory", {})["graph_findings"] = [{"tool": s["tool"], "rows": (s["result"].get("rows") or [])[:6]} for s in result["steps"]]
    # Whatever the agent changed (decisions, new complaints) comes back from FalkorDB itself.
    known = {n["data"]["id"] for n in sess["nodes"]}
    known_e = {e["data"]["id"] for e in sess["edges"]}
    view = user_graph.falkor_elements(entity_id("idea", idea_raw))
    if view:
        d_nodes = [n for n in view["nodes"] if n["data"]["id"] not in known]
        d_edges = [e for e in view["edges"] if e["data"]["id"] not in known_e]
    else:
        d_nodes = [{"data": {**d, "version": 0}} for d in user_graph._mem_nodes.values() if d.get("type") == "decision" and d["id"] not in known]
        d_ids = {d["data"]["id"] for d in d_nodes}
        d_edges = [{"data": {k: e[k] for k in ("id", "source", "target", "label")}} for e in user_graph._mem_edges.values() if e["source"] in d_ids]
    if d_nodes:
        sess["nodes"].extend(d_nodes)
        sess["edges"].extend(d_edges)
        yield f"data: {json.dumps({'type': 'graph_delta', 'nodes': d_nodes, 'edges': d_edges, 'superseded': []})}\n\n"
    sess["memory"]["agent_final"] = result.get("final", "")


async def _followup(sess: Dict[str, Any], text: str, user_key: str, header: str = "", session_id: Optional[str] = None) -> Optional[str]:
    """Memory-aware reply for turn 2+; records both sides of the turn. None when no LLM is available."""
    mem = sess.setdefault("memory", {"idea": sess["lineage"][0]["delta"] if sess.get("lineage") else "", "transcript": []})
    res = await asyncio.to_thread(continue_idea_chat, text, mem, get_profile(user_key), not header)  # no header = a question turn
    _remember(sess, "user", text, session_id)
    reply = res.get("reply") if res else None
    if res:
        mem["idea_state"] = res.get("idea_update", mem.get("idea_state", ""))
        set_profile(user_key, res.get("user_profile", ""))
        if isinstance(res.get("action_plan"), list) and res["action_plan"]:
            sess["plan"] = res["action_plan"]
        sess["version_title"] = str(res.get("version_title") or "")[:40]
        mem["brief"] = merge_brief(mem.get("brief"), res.get("brief"))
        mem["brief_question"] = next_question(mem["brief"], str(res.get("next_question") or ""))[:400]
        if reply:
            reply += "\n\n" + brief_md(mem["brief"], mem["brief_question"])
    if reply and header:
        reply = f"*{header}*\n\n{reply}"
    if not reply and llm_unavailable_note():
        reply = f"*{header}*\n\n" + followup_fallback(text, mem.get("graph_findings") or [], sess.get("plan") or []) + llm_unavailable_note()
    _remember(sess, "assistant", reply or header, session_id)
    return reply



@app.post("/api/analyze")
async def analyze_endpoint(payload: AnalyzeRequest, request: Request):
    """
    Observable SSE pipeline stream tested by test_sse_pipeline_stream_observable.
    Emits LOG_STREAM, GRAPH_INGEST, CYPHER_TRAVERSAL, TABULAR_EVAL, CROSS_SESSION_MEMORY, PIPELINE_COMPLETE.
    """
    if auth_enabled():
        require_user(request)
    problem = guard.check_message(payload.idea, True)
    if problem:
        return _refuse(400, problem)
    if not os.getenv("PYTEST_CURRENT_TEST") and guard.new_ideas.retry_after(_client_key(request)):
        return _refuse(429, "Limit reached (3 new ideas per hour).")

    async def pipeline_stream():
        idea = payload.idea
        u_id = f"idea_{entity_id('pipeline', idea)}"

        # 1. LOG_STREAM
        market_data = await asyncio.to_thread(search_market, idea)
        badge = "LIVE_SERPAPI" if market_data.get("source_mode") == "LIVE_SERPAPI" else "DETERMINISTIC_FALLBACK"
        yield f"data: {json.dumps({'stage': 'LOG_STREAM', 'source_badge': badge, 'competitors': market_data.get('competitors', []), 'complaints': market_data.get('complaints', [])})}\n\n"
        await asyncio.sleep(0.05)

        # 2. Ingest
        graph.insert_market_nodes(u_id, idea, market_data.get("competitors", []), market_data.get("complaints", []))

        # 3. GRAPH_INGEST
        cytoscape_elements = graph.get_full_graph_elements(u_id)
        yield f"data: {json.dumps({'stage': 'GRAPH_INGEST', 'cytoscape': cytoscape_elements})}\n\n"
        await asyncio.sleep(0.05)

        # 4. CYPHER_TRAVERSAL
        wedge_data, meta = graph.query_unserved_wedge_with_meta(u_id)
        top_feature = wedge_data[0]["feature"] if wedge_data else "Local-First Core"
        yield f"data: {json.dumps({'stage': 'CYPHER_TRAVERSAL', 'cypher_query': meta['cypher_query'], 'unserved_wedge_feature': top_feature, 'rows': len(wedge_data)})}\n\n"
        await asyncio.sleep(0.05)

        # 5. TABULAR_EVAL
        comps = market_data.get("competitors", [])
        complaints = market_data.get("complaints", [])
        first_price = float(comps[0].get("price_usd", 49.0)) if comps else 49.0
        tab_res = evaluate_market_saturation(
            competitor_count=len(comps),
            median_price_usd=first_price,
            complaint_count=len(complaints),
            has_open_source=1,
            review_volume=250
        )
        yield f"data: {json.dumps({'stage': 'TABULAR_EVAL', 'feature_vector': {'competitors': len(comps)}, 'saturation_index': tab_res['saturation_index'], 'risk_tier': tab_res['risk_tier']})}\n\n"
        await asyncio.sleep(0.05)

        # 6. CROSS_SESSION_MEMORY
        ledger = graph.get_memory_ledger()
        yield f"data: {json.dumps({'stage': 'CROSS_SESSION_MEMORY', 'memory_ledger': ledger, 'past_sessions': ledger.get('historical_ideas', [])})}\n\n"
        await asyncio.sleep(0.05)

        # 7. PIPELINE_COMPLETE
        blueprint = synthesize_attack_blueprint(idea, wedge_data, tab_res)
        yield f"data: {json.dumps({'stage': 'PIPELINE_COMPLETE', 'blueprint': blueprint})}\n\n"

    return StreamingResponse(pipeline_stream(), media_type="text/event-stream")


def _client_key(request: Request) -> str:
    user = current_user(request)
    if user and user.get("login") != "guest":
        return f"user:{user['login']}"
    # Behind Render's proxy the real client is the hop the proxy appended (rightmost); elsewhere use the socket peer.
    # shortcut: assumes exactly one trusted proxy hop on Render; revisit if a CDN is put in front.
    fwd = request.headers.get("x-forwarded-for", "") if os.getenv("RENDER") else ""
    return f"ip:{fwd.split(',')[-1].strip() or (request.client.host if request.client else 'unknown')}"


def _refuse(status: int, text: str) -> JSONResponse:
    return JSONResponse({"detail": text}, status_code=status)


@app.post("/api/chat")
async def chat_endpoint(payload: ChatRequest, request: Request):
    if auth_enabled():
        require_user(request)
    owner = _owner(request)
    sess = load_session(payload.session_id)
    if sess and sess.get("owner") not in (None, owner):
        return _refuse(403, "This session belongs to someone else. Start a new session.")
    has_lineage = bool(sess and sess.get("lineage"))
    new_idea = not has_lineage or classify_intent(payload.message.strip(), has_lineage=True).get("kind") == "new_concept" \
        or payload.message.strip().lower().startswith("new idea:")
    problem = guard.check_message(payload.message, new_idea)
    if problem:
        return _refuse(400, problem)
    if not os.getenv("PYTEST_CURRENT_TEST"):
        key = _client_key(request)
        wait = guard.messages.retry_after(key) or (guard.new_ideas.retry_after(key) if new_idea else 0)
        if wait:
            what = "3 new ideas per hour" if new_idea else "20 messages per 10 minutes"
            return _refuse(429, f"Limit reached ({what}). Try again in {max(1, wait // 60)} min.")
    return StreamingResponse(
        chat_event_stream(payload.session_id, payload.message.strip(), owner, owner),
        media_type="text/event-stream"
    )


@app.post("/api/tts")
async def text_to_speech(payload: Dict[str, str], request: Request):
    """ElevenLabs Voice Narration API endpoint."""
    text = payload.get("text", "")
    if not os.getenv("PYTEST_CURRENT_TEST") and guard.messages.retry_after(_client_key(request)):
        return _refuse(429, "Too many requests. Try again in a few minutes.")
    api_key = settings.elevenlabs_api_key or os.getenv("ELEVENLABS_API_KEY")
    if not api_key:
        return HTMLResponse(content="ELEVENLABS_API_KEY not configured", status_code=400)

    try:
        import urllib.request
        voice_id = "JBFqnCBsd6RMkjVDRZzb"
        tts_url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
        req_data = json.dumps({
            "text": text[:900],
            "model_id": "eleven_multilingual_v2",
            # lower stability = more natural, conversational delivery; speaker boost keeps the voice clear
            "voice_settings": {"stability": 0.4, "similarity_boost": 0.8, "style": 0.3, "use_speaker_boost": True}
        }).encode("utf-8")
        req = urllib.request.Request(
            tts_url,
            data=req_data,
            headers={
                "Accept": "audio/mpeg",
                "Content-Type": "application/json",
                "xi-api-key": api_key
            },
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=30.0) as resp:
            audio_bytes = resp.read()
            return StreamingResponse(iter([audio_bytes]), media_type="audio/mpeg")
    except Exception as e:
        return HTMLResponse(content=f"TTS Generation failed: {str(e)}", status_code=500)


frontend_dir = Path(__file__).resolve().parent.parent / "frontend"
if not frontend_dir.exists():
    frontend_dir = Path(__file__).resolve().parent / "frontend"

app.mount("/static", StaticFiles(directory=str(frontend_dir)), name="static")


@app.get("/", response_class=HTMLResponse)
def get_index():
    chat_file = frontend_dir / "chat.html"
    if chat_file.exists():
        return HTMLResponse(content=chat_file.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Chat UI not found</h1>", status_code=404)


@app.get("/pipeline", response_class=HTMLResponse)
def get_pipeline_page():
    pipe_file = frontend_dir / "index.html"
    if pipe_file.exists():
        return HTMLResponse(content=pipe_file.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Pipeline UI not found</h1>", status_code=404)
