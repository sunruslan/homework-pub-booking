"""Simulate Ex7 production failure: incomplete handoff IPC payload.

The executor calls handoff_to_structured with venue_search metadata in `data`
instead of confirm_booking fields (no venue_id). The bridge rejects before Rasa;
the loop retries with the same mistake until max_rounds.

Run:  uv run python scripts/simulate_q3_ipc_failure.py
"""

from __future__ import annotations

import asyncio
import json
import sys

from sovereign_agent._internal.llm_client import FakeLLMClient, ScriptedResponse, ToolCall
from sovereign_agent._internal.paths import example_sessions_dir
from sovereign_agent.executor import DefaultExecutor
from sovereign_agent.halves.loop import LoopHalf
from sovereign_agent.planner import DefaultPlanner
from sovereign_agent.session.directory import create_session

from starter.edinburgh_research.tools import build_tool_registry
from starter.handoff_bridge.bridge import HandoffBridge
from starter.handoff_bridge.run import EX7_TASK
from starter.rasa_half.structured_half import RasaStructuredHalf, spawn_mock_rasa

_BAD_HANDOFF_DATA = {
    "search_results": {
        "near": "Haymarket",
        "party_size": 12,
        "results": [],
        "area_venues": [
            {
                "id": "haymarket_tap",
                "name": "Haymarket Tap",
                "seats_available_evening": 8,
                "blocked_by": ["party_size"],
            }
        ],
    },
    "suggested_actions": ["Reduce party size to 8", "Search Old Town"],
}


def _build_bad_handoff_client() -> FakeLLMClient:
    plan = json.dumps(
        [
            {
                "id": "sg_1",
                "description": "search Haymarket for 12",
                "success_criterion": "venue_search ran",
                "estimated_tool_calls": 2,
                "depends_on": [],
                "assigned_half": "loop",
            }
        ]
    )
    search = ScriptedResponse(
        tool_calls=[
            ToolCall(
                id="c1",
                name="venue_search",
                arguments={"near": "Haymarket", "party_size": 12, "budget_max_gbp": 1000},
            )
        ]
    )
    bad_handoff = ScriptedResponse(
        tool_calls=[
            ToolCall(
                id="c2",
                name="handoff_to_structured",
                arguments={
                    "reason": "No Haymarket venue fits 12 — escalating to structured",
                    "context": "venue_search returned empty results list",
                    "data": dict(_BAD_HANDOFF_DATA),
                },
            )
        ]
    )
    # Three identical rounds (planner + search + bad handoff) × 3
    scripted: list = []
    for _ in range(3):
        scripted.extend([ScriptedResponse(content=plan), search, bad_handoff])
    return FakeLLMClient(scripted)


async def main() -> int:
    with example_sessions_dir("ex7-handoff-bridge", persist=True) as sessions_root:
        session = create_session(
            scenario="ex7-handoff-bridge",
            task=EX7_TASK,
            sessions_dir=sessions_root,
        )
        print(f"Session {session.session_id}")
        print(f"  dir: {session.directory}")

        server, _thread, mock_url = spawn_mock_rasa(port=5907)
        client = _build_bad_handoff_client()
        try:
            bridge = HandoffBridge(
                loop_half=LoopHalf(
                    planner=DefaultPlanner(model="fake", client=client),
                    executor=DefaultExecutor(
                        model="fake",
                        client=client,
                        tools=build_tool_registry(session),
                    ),
                ),
                structured_half=RasaStructuredHalf(rasa_url=mock_url),
                max_rounds=3,
            )
            result = await bridge.run(session, {"task": EX7_TASK})
        finally:
            server.shutdown()

        print(f"\nBridge outcome: {result.outcome}")
        print(f"  rounds: {result.rounds}")
        print(f"  summary: {result.summary}")
        return 0 if result.outcome == "max_rounds_exceeded" else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
