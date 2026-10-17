# GitHub Copilot Custom Instructions for Ideator

## Project Overview
Ideator is a multi-agent adversarial market validation system built for Hackathon Track 01, Track 02, Sentry Agent Tracing, Temporal Durable Execution, and FalkorDB Graph Reasoning.

## Architecture
- **Backend**: FastAPI + Python 3.12 (`ideator/backend/`)
- **Knowledge Graph**: FalkorDB Cypher graph (`MarketGraph` in `graph.py`) with zero-crash in-memory mirror and PageRank / Community Detection graph algorithms.
- **Agents**: Dual-agent system (`ResearcherAgent` and `StrategistAgent` in `agent.py`).
- **Telemetry**: Sentry Agent Tracing with `ai.agent`, `ai.tool_call`, and `ai.run` spans (`telemetry.py`).
- **Durability**: Temporal Workflow and Activities (`workflow.py`) with exponential retries and in-process fallback.
- **Frontend**: SSE-streamed interactive Cytoscape graph console (`chat.html`, `chat.js`).

## Coding Guidelines for Copilot
1. **TDD Required**: Always write and run tests under `tests/` before declaring work complete.
2. **Zero-Crash In-Memory Fallback**: Never assume external services (FalkorDB, SerpAPI, TabPFN, Gemma, Temporal) are live. Always provide safe fallbacks so the app and tests run anywhere without crashing.
3. **No Redundant Aliases**: Use canonical key names (`confidence`, `recommended_entry_price`).
4. **Ponytail Principles**: Choose standard library and installed dependencies first. Keep diffs concise and eliminate dead code.
