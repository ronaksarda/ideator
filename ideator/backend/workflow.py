import asyncio
import logging
import os
from datetime import timedelta
from typing import Any, Dict

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

logger = logging.getLogger("ideator.workflow")

with workflow.unsafe.imports_passed_through():  # app modules do I/O; the workflow sandbox must not re-run them
    try:
        from backend.agent import ResearcherAgent, StrategistAgent, build_idea_report
        from backend.config import settings
        from backend.graph import MarketGraph, entity_id
        from backend.search import search_market
        from backend.tabular import evaluate_market_saturation
        from backend.telemetry import agent_span
    except ImportError:
        from ideator.backend.agent import ResearcherAgent, StrategistAgent, build_idea_report
        from ideator.backend.config import settings
        from ideator.backend.graph import MarketGraph, entity_id
        from ideator.backend.search import search_market
        from ideator.backend.tabular import evaluate_market_saturation
        from ideator.backend.telemetry import agent_span


# ---- Durable turn steps used by the chat app ----
# The two steps that call flaky paid services (web search, Gemma report) run as Temporal activities: retried with
# backoff, and visible in the Temporal UI (http://localhost:8233). Without a Temporal server they run directly.

RETRY = RetryPolicy(initial_interval=timedelta(seconds=2), backoff_coefficient=2.0, maximum_attempts=3)
TASK_QUEUE = "ideator-chat"
_client: Dict[str, Any] = {}


@activity.defn
def search_activity(idea_text: str) -> Dict[str, Any]:
    return search_market(idea_text)


@activity.defn
def report_activity(args: Dict[str, Any]) -> Dict[str, Any]:
    from temporalio.exceptions import ApplicationError
    report = build_idea_report(args["idea_text"], args["market"], args["tab"], args.get("user_profile", ""))
    if not report:
        raise ApplicationError("Gemma returned no usable report")  # makes Temporal retry the call
    return report


@workflow.defn
class SearchWorkflow:
    @workflow.run
    async def run(self, idea_text: str) -> Dict[str, Any]:
        return await workflow.execute_activity(search_activity, idea_text, start_to_close_timeout=timedelta(seconds=120), retry_policy=RETRY)


@workflow.defn
class ReportWorkflow:
    @workflow.run
    async def run(self, args: Dict[str, Any]) -> Dict[str, Any]:
        return await workflow.execute_activity(report_activity, args, start_to_close_timeout=timedelta(seconds=150), retry_policy=RETRY)


async def start_worker() -> bool:
    """Connect to Temporal and run a worker inside the app. False (and direct calls) when no server is reachable."""
    try:
        from concurrent.futures import ThreadPoolExecutor
        from temporalio.client import Client
        from temporalio.worker import Worker
        client = await asyncio.wait_for(Client.connect(os.getenv("TEMPORAL_HOST", "localhost:7233")), timeout=3)
        worker = Worker(client, task_queue=TASK_QUEUE, workflows=[SearchWorkflow, ReportWorkflow],
                        activities=[search_activity, report_activity], activity_executor=ThreadPoolExecutor(8))
        _client["worker"] = asyncio.create_task(worker.run())
        _client["client"] = client
        logger.info("Temporal worker running on %s", TASK_QUEUE)
        return True
    except Exception as e:
        logger.warning("Temporal unavailable, running steps directly: %s", e)
        return False


async def durable(wf_run, arg: Any, wf_id: str, direct):
    """Run through Temporal when connected; otherwise (or if the workflow fails after retries) call `direct`."""
    client = _client.get("client")
    if client:
        try:
            return await client.execute_workflow(wf_run, arg, id=wf_id, task_queue=TASK_QUEUE)
        except Exception as e:
            logger.warning("Workflow %s failed after retries: %s", wf_id, e)
            return None if wf_run is ReportWorkflow.run else await asyncio.to_thread(direct)
    return await asyncio.to_thread(direct)
