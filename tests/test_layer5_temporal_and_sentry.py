import os
import pytest
from ideator.backend.telemetry import agent_span
from ideator.backend.workflow import ReportWorkflow, SearchWorkflow, durable, report_activity, search_activity


def test_sentry_agent_tracing_spans():
    # The span helper must work without a Sentry DSN
    with agent_span("ai.agent", "ResearcherAgent", {"idea": "SQLite API"}) as span:
        assert True

    with agent_span("ai.tool_call", "FalkorDB.query", {"cypher": "MATCH (n) RETURN n"}) as span:
        pass


def test_temporal_workflow_and_activities_defined():
    assert hasattr(SearchWorkflow, "__temporal_workflow_definition")
    assert hasattr(ReportWorkflow, "__temporal_workflow_definition")
    assert hasattr(search_activity, "__temporal_activity_definition")
    assert hasattr(report_activity, "__temporal_activity_definition")


def test_durable_runs_directly_without_temporal():
    import asyncio
    # No worker connected in tests: the step runs in-process instead of hanging on a Temporal queue.
    assert asyncio.run(durable(SearchWorkflow.run, "x", "search-test", lambda: {"ok": True})) == {"ok": True}


def test_ci_workflow_present():
    assert os.path.exists(".github/workflows/ci.yml")
