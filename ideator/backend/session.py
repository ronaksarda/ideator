"""Multi-turn idea memory.

Every turn reads the session's idea lineage and market sub-graph back out of
FalkorDB (or the in-memory mirror when FalkorDB is down). Nothing about the
conversation is kept in an LLM context: the graph is the state.

Version topology:
    (:UserSession)-[:ACTIVE_ROOT]->(:UserIdea {version: 1})
    (:UserSession)-[:ACTIVE]->(:UserIdea)            current head
    (:UserIdea vN)-[:EVOLVED_FROM]->(:UserIdea vN-1)
    (:UserIdea vN)-[:SUPERSEDES]->(:UserIdea)        head the user abandoned
    (:UserIdea)-[:SOLVES]->(:Grievance)
    (:UserIdea)-[:OMITS]->(:Feature)
"""
import re
import statistics
import time
from typing import Any, Dict, List, Optional, Set, Tuple

try:
    from backend.graph import MarketGraph, entity_id
except ImportError:
    from ideator.backend.graph import MarketGraph, entity_id


# Concept groups used to link free text to grievance / feature nodes.
# A message and a node match when they hit the same group.
CONCEPT_GROUPS: Dict[str, Set[str]] = {
    "cloud": {
        "cloud", "sync", "egress", "telemetry", "lock-in", "lockin", "vendor",
        "saas", "local", "local-first", "local-only", "offline", "sqlite",
        "self-hosted", "selfhosted", "on-prem", "p2p", "peer", "usb", "lagging",
        "proprietary", "drops",
    },
    "price": {
        "price", "pricing", "priced", "cheap", "cheaper", "free", "cost",
        "expensive", "minimum", "overpriced", "insane", "tier", "invert",
        "undercut", "$",
    },
    "setup": {"setup", "onboarding", "install", "days", "minutes", "simple", "zero-config"},
    "webhook": {"webhook", "webhooks", "stripe", "schema", "payload", "retry", "fails"},
    "tenant": {"tenant", "multi-tenant", "enterprise", "rbac", "team", "teams", "seats",
               "solo", "indie", "individual", "freelancer", "freelancers"},
}

OMIT_VERBS = {"cut", "drop", "dropping", "strip", "stripping", "remove", "removing",
              "without", "kill", "ditch", "skip", "no", "omit", "lose"}
PIVOT_MARKERS = ("what if", "let's", "lets ", "make it", "add ", "switch", "pivot",
                 "target", "instead", "focus", "go ", "build", "use ")
QUESTION_STARTS = ("what", "how", "which", "should", "why", "who", "where", "is ", "are ", "can ", "do ")
REVERT_RE = re.compile(r"\b(drop|scrap|forget|undo|revert|ditch)\s+(that|this|it|the last)\b|\binstead\b")
NEW_IDEA_RE = re.compile(r"^\s*(new idea|different idea|start over|new concept)\s*[:\-]?\s*", re.I)
PRICE_RE = re.compile(r"\$\s?(\d+(?:\.\d+)?)")


def _tokens(text: str) -> Set[str]:
    raw = re.findall(r"[a-z0-9$][a-z0-9\-$]*", text.lower())
    out = set(raw)
    for t in raw:
        out.update(p for p in t.split("-") if p)
    if "$" in text:
        out.add("$")
    return out


def concept_hits(text: str) -> Set[str]:
    toks = _tokens(text)
    return {g for g, words in CONCEPT_GROUPS.items() if toks & words}


def classify_intent(message: str, has_lineage: bool) -> Dict[str, Any]:
    """Rule-based intent router. Deterministic, so it never burns tokens."""
    text = message.strip()
    low = text.lower()
    new_match = NEW_IDEA_RE.match(text)
    if not has_lineage or new_match:
        concept = text[new_match.end():].strip() if new_match else text
        return {"kind": "new_concept", "concept": concept or text}

    toks = _tokens(low)
    is_pivot = any(m in low for m in PIVOT_MARKERS) or bool(toks & OMIT_VERBS)
    is_question = low.endswith("?") or low.startswith(QUESTION_STARTS)
    if is_question and not is_pivot:
        return {"kind": "question", "concept": text}

    price = PRICE_RE.search(text)
    return {
        "kind": "refine",
        "concept": text,
        "revert": bool(REVERT_RE.search(low)),
        "omit": bool(toks & OMIT_VERBS),
        "price_usd": float(price.group(1)) if price else (0.0 if re.search(r"\bfree\b", low) else None),
    }


class IdeaMemory:
    """Session + version layer on top of MarketGraph."""

    def __init__(self, graph: MarketGraph):
        self.graph = graph
        # Mirror used only when FalkorDB is unreachable.
        self._sessions: Dict[str, Dict[str, Any]] = {}
        self._versions: Dict[str, Dict[str, Any]] = {}
        self._edges: List[Tuple[str, str, str]] = []  # (source, relation, target)
        if self.graph.is_live:
            for idx in ("CREATE INDEX ON :UserSession(id)", "CREATE INDEX ON :UserIdea(root_id)"):
                self._run(idx)

    # ---- low-level ---------------------------------------------------------

    def _run(self, query: str, params: Optional[Dict[str, Any]] = None):
        if not (self.graph.is_live and self.graph._graph):
            return None
        try:
            return self.graph._graph.query(query, params or {})
        except Exception:
            return None

    # ---- writes ------------------------------------------------------------

    def create_session(self, session_id: str, root_uid: str, concept: str,
                       previous: Optional[str] = None) -> Tuple[Dict[str, Any], List[str]]:
        now = int(time.time())
        v1 = {"id": root_uid, "root_id": root_uid, "version": 1, "concept": concept,
              "delta": "Initial concept", "ts": now, "parent": None, "intent": "new_concept",
              "price_usd": None}
        self._sessions[session_id] = {"id": session_id, "created_at": now, "root": root_uid, "active": root_uid}
        self._versions[root_uid] = v1
        if previous:
            self._edges.append((root_uid, "SUPERSEDES", previous))
        self._run("MATCH (s:UserSession {id: $sid})-[r:ACTIVE_ROOT|ACTIVE]->() DELETE r", {"sid": session_id})
        q = ("MERGE (s:UserSession {id: $sid}) SET s.created_at = $now "
             "WITH s MATCH (u:UserIdea {id: $uid}) "
             "SET u.version = 1, u.root_id = $uid, u.concept = $concept, u.delta = $delta, u.ts = $now "
             "MERGE (s)-[:ACTIVE_ROOT]->(u) MERGE (s)-[:ACTIVE]->(u)")
        self._run(q, {"sid": session_id, "now": now, "uid": root_uid, "concept": concept, "delta": v1["delta"]})
        stmts = [q]
        if previous:
            q2 = "MATCH (u:UserIdea {id: $uid}), (o:UserIdea {id: $old}) MERGE (u)-[:SUPERSEDES]->(o)"
            self._run(q2, {"uid": root_uid, "old": previous})
            stmts.append(q2)
        return v1, stmts

    def create_version(
        self,
        session_id: str,
        parent: Dict[str, Any],
        superseded: Optional[Dict[str, Any]],
        concept: str,
        delta: str,
        intent: str,
        solves: List[str],
        omits: List[str],
        price_usd: Optional[float],
        next_version: int,
    ) -> Tuple[Dict[str, Any], List[str]]:
        root = parent["root_id"]
        uid = f"{root}_v{next_version}"
        now = int(time.time())
        v = {"id": uid, "root_id": root, "version": next_version, "concept": concept, "delta": delta,
             "ts": now, "parent": parent["id"], "intent": intent, "price_usd": price_usd}
        self._versions[uid] = v
        self._sessions[session_id]["active"] = uid
        self._edges.append((uid, "EVOLVED_FROM", parent["id"]))
        if superseded:
            self._edges.append((uid, "SUPERSEDES", superseded["id"]))
        self._edges += [(uid, "SOLVES", g) for g in solves] + [(uid, "OMITS", f) for f in omits]

        stmts = [
            "MATCH (p:UserIdea {id: $parent}) "
            "CREATE (u:UserIdea {id: $uid, root_id: $root, version: $ver, concept: $concept, "
            "delta: $delta, intent: $intent, price_usd: $price, ts: $now})-[:EVOLVED_FROM]->(p)",
            "MATCH (s:UserSession {id: $sid})-[a:ACTIVE]->() DELETE a",
            "MATCH (s:UserSession {id: $sid}), (u:UserIdea {id: $uid}) CREATE (s)-[:ACTIVE]->(u)",
        ]
        params = {"parent": parent["id"], "uid": uid, "root": root, "ver": next_version, "concept": concept,
                  "delta": delta, "intent": intent, "price": price_usd if price_usd is not None else -1.0,
                  "now": now, "sid": session_id}
        for q in stmts:
            self._run(q, params)
        if superseded:
            q = "MATCH (u:UserIdea {id: $uid}), (o:UserIdea {id: $old}) CREATE (u)-[:SUPERSEDES]->(o)"
            self._run(q, {"uid": uid, "old": superseded["id"]})
            stmts.append(q)
        if solves:
            q = "MATCH (u:UserIdea {id: $uid}) UNWIND $ids AS gid MATCH (g:Grievance {id: gid}) CREATE (u)-[:SOLVES]->(g)"
            self._run(q, {"uid": uid, "ids": solves})
            stmts.append(q)
        if omits:
            q = "MATCH (u:UserIdea {id: $uid}) UNWIND $ids AS fid MATCH (f:Feature {id: fid}) CREATE (u)-[:OMITS]->(f)"
            self._run(q, {"uid": uid, "ids": omits})
            stmts.append(q)
        return v, stmts

    # ---- reads -------------------------------------------------------------

    def load_lineage(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Pull the session's versions and their SOLVES/OMITS edges out of the graph."""
        res = self._run(
            "MATCH (s:UserSession {id: $sid})-[:ACTIVE_ROOT]->(r:UserIdea) "
            "MATCH (s)-[:ACTIVE]->(a:UserIdea) RETURN r.id, a.id",
            {"sid": session_id},
        )
        if res is not None and res.result_set:
            root, active = res.result_set[0]
            rows = self._run(
                "MATCH (v:UserIdea {root_id: $root}) OPTIONAL MATCH (v)-[:EVOLVED_FROM]->(p:UserIdea) "
                "RETURN v.id, v.version, v.concept, v.delta, v.intent, v.price_usd, v.ts, p.id",
                {"root": root},
            )
            edges = self._run(
                "MATCH (v:UserIdea {root_id: $root})-[r]->(t) "
                "WHERE type(r) IN ['SOLVES', 'OMITS', 'SUPERSEDES'] RETURN v.id, type(r), t.id",
                {"root": root},
            )
            if rows is not None and edges is not None:
                versions = {}
                for vid, ver, concept, delta, intent, price, ts, parent in rows.result_set:
                    versions[vid] = {"id": vid, "root_id": root, "version": int(ver), "concept": concept,
                                     "delta": delta, "intent": intent or "new_concept",
                                     "price_usd": None if price is None or price < 0 else float(price),
                                     "ts": ts, "parent": parent}
                return {"root": root, "active": active, "versions": versions,
                        "edges": [tuple(e) for e in edges.result_set], "engine": "FalkorDB"}

        sess = self._sessions.get(session_id)
        if not sess:
            return None
        versions = {k: v for k, v in self._versions.items() if v["root_id"] == sess["root"]}
        edges = [e for e in self._edges if e[0] in versions and e[1] != "EVOLVED_FROM"]
        return {"root": sess["root"], "active": sess["active"], "versions": versions,
                "edges": edges, "engine": "in-memory mirror"}

    def market_view(self, root_uid: str) -> Dict[str, Any]:
        """Competitors, grievances, features and prices the root idea targets."""
        g = self.graph
        comps: Dict[str, Dict[str, Any]] = {}
        griev: Dict[str, Dict[str, Any]] = {}
        feats: Dict[str, Dict[str, Any]] = {}

        live = self._run(
            "MATCH (u:UserIdea {id: $u})-[:TARGETS]->(c:Competitor) "
            "OPTIONAL MATCH (c)-[:PRICED_AT]->(p:Pricing) RETURN c.id, c.name, p.amount_usd",
            {"u": root_uid},
        )
        if live is not None and live.result_set:
            for cid, name, price in live.result_set:
                comps[cid] = {"id": cid, "name": name, "price": None if price is None else float(price), "features": set()}
            for cid, fid, fname in self._run(
                "MATCH (u:UserIdea {id: $u})-[:TARGETS]->(c)-[:OFFERS]->(f:Feature) RETURN c.id, f.id, f.name",
                {"u": root_uid},
            ).result_set:
                comps[cid]["features"].add(fid)
                feats[fid] = {"id": fid, "name": fname}
            for cid, gid, text, fid, fname in self._run(
                "MATCH (u:UserIdea {id: $u})-[:TARGETS]->(c)-[:DRAWS_COMPLAINT]->(g:Grievance)"
                "-[:TARGETS_FEATURE]->(f:Feature) RETURN c.id, g.id, g.text, f.id, f.name",
                {"u": root_uid},
            ).result_set:
                griev[gid] = {"id": gid, "text": text, "competitor": cid, "feature": fid}
                feats.setdefault(fid, {"id": fid, "name": fname})
            return {"competitors": comps, "grievances": griev, "features": feats}

        nodes, edges = g._mem_nodes, g._mem_edges.values()
        for e in edges:
            if e["source"] == root_uid and e["label"] == "TARGETS":
                cid = e["target"]
                comps[cid] = {"id": cid, "name": nodes[cid]["label"], "price": None, "features": set()}
        for e in edges:
            src, tgt, lbl = e["source"], e["target"], e["label"]
            if src not in comps:
                continue
            if lbl == "PRICED_AT":
                comps[src]["price"] = float(nodes[tgt]["amount_usd"])
            elif lbl == "OFFERS":
                comps[src]["features"].add(tgt)
                feats[tgt] = {"id": tgt, "name": nodes[tgt]["label"]}
            elif lbl == "DRAWS_COMPLAINT":
                griev[tgt] = {"id": tgt, "text": nodes[tgt].get("full_text", nodes[tgt]["label"]),
                              "competitor": src, "feature": None}
        for e in edges:
            if e["source"] in griev and e["label"] == "TARGETS_FEATURE":
                griev[e["source"]]["feature"] = e["target"]
                feats.setdefault(e["target"], {"id": e["target"], "name": nodes[e["target"]]["label"]})
        return {"competitors": comps, "grievances": griev, "features": feats}


# ---- pure helpers (no I/O, easy to test) ------------------------------------

def chain(lineage: Dict[str, Any], vid: str) -> List[str]:
    """Version ids from vid back to the root, following EVOLVED_FROM."""
    out, seen = [], set()
    while vid and vid not in seen:
        seen.add(vid)
        out.append(vid)
        vid = lineage["versions"].get(vid, {}).get("parent")
    return out


def cumulative_edges(lineage: Dict[str, Any], vid: str) -> Dict[str, Set[str]]:
    ids = set(chain(lineage, vid))
    solves = {t for s, r, t in lineage["edges"] if s in ids and r == "SOLVES"}
    omits = {t for s, r, t in lineage["edges"] if s in ids and r == "OMITS"}
    return {"solves": solves, "omits": omits}


def effective_price(lineage: Dict[str, Any], vid: str) -> Optional[float]:
    for v in chain(lineage, vid):
        p = lineage["versions"][v].get("price_usd")
        if p is not None:
            return p
    return None


def match_targets(message: str, market: Dict[str, Any], omit: bool) -> Tuple[List[str], List[str]]:
    """Grievances the message addresses, and features it cuts."""
    groups = concept_hits(message)
    if not groups:
        return [], []
    feat_names = {fid: f["name"] for fid, f in market["features"].items()}
    solves = []
    for gid, g in market["grievances"].items():
        node_groups = concept_hits(g["text"] + " " + feat_names.get(g["feature"], ""))
        if groups & node_groups:
            solves.append(gid)
    omits = []
    if omit:
        for fid, name in feat_names.items():
            # Only cut features that draw complaints the message is about.
            complained = [g for g in market["grievances"].values() if g["feature"] == fid]
            if complained and groups & concept_hits(name + " " + " ".join(g["text"] for g in complained)):
                omits.append(fid)
    return sorted(solves), sorted(omits)


def feature_vector(market: Dict[str, Any], solves: Set[str], omits: Set[str],
                   price: Optional[float]) -> Dict[str, Any]:
    """Tabular inputs for one idea version.

    A competitor stays "coupled" (still a head-on rival) unless the idea solves
    one of its grievances or omits a feature its users complain about.
    Each solved grievance counts as captured demand (weight 3).
    """
    comps = market["competitors"]
    decoupled = set()
    for g in market["grievances"].values():
        if g["id"] in solves or g["feature"] in omits:
            decoupled.add(g["competitor"])
    coupled = [c for c in comps if c not in decoupled]
    prices = [c["price"] for c in comps.values() if c["price"]]  # drops unknown (None) and free (0)
    median = statistics.median(prices) if prices else None
    return {
        "competitor_count": len(coupled) if solves or omits else len(comps),
        "median_price_usd": price if median is None else min(median, price) if price is not None else median,
        "complaint_count": len(market["grievances"]) + 3 * len(solves),
        "has_open_source": 1,
        "decoupled_competitors": sorted(comps[c]["name"] for c in decoupled),
    }
