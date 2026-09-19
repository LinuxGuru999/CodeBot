"""Agent testing harness for CodeBot role prompts.

Provides a deterministic execution environment to test LLM-driven agents
without real API calls. Replaces the model with a scripted response queue,
executes tools through the real api_runner tool pipeline, and captures all
interactions for assertion.

Usage:
    from tests.agent_harness import ScriptedModel, run_agent_loop, AgentResult

    model = ScriptedModel([
        tool_response("read", {"path": "/tmp/test.json"}, call_id="1"),
        content_response("done"),
    ])
    result = run_agent_loop("test_bot", "do stuff", model, tmp_path)
    assert result.tool_call_count == 1
    assert result.exit_reason == "completed"
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Ensure codebot is importable
sys.path.insert(0, str(Path(__file__).parent.parent))

from codebot.api_runner import _execute_tool, _parse_tool_args

MAX_ITERATIONS = 50


@dataclass
class ToolCallRecord:
    """Single tool invocation captured during agent execution."""
    name: str
    args: dict[str, Any]
    result: dict[str, Any]
    iteration: int


@dataclass
class AgentResult:
    """Complete record of an agent loop execution."""
    bot_name: str
    exit_reason: str
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    messages: list[dict[str, Any]] = field(default_factory=list)
    iterations: int = 0
    tickets_created: int = 0
    files_touched: list[str] = field(default_factory=list)

    @property
    def tool_call_count(self) -> int:
        return len(self.tool_calls)

    @property
    def tool_names(self) -> list[str]:
        return [tc.name for tc in self.tool_calls]

    def calls_to(self, tool_name: str) -> list[ToolCallRecord]:
        """Return all records where the named tool was called."""
        return [tc for tc in self.tool_calls if tc.name == tool_name]

    def create_ticket_args(self) -> list[dict[str, Any]]:
        """Return arguments from all create_ticket calls."""
        return [tc.args for tc in self.calls_to("create_ticket")]

    def successful_ticket_creations(self) -> int:
        """Count create_ticket calls that returned success=True."""
        return sum(
            1 for tc in self.calls_to("create_ticket")
            if tc.result.get("success")
        )

    def has_forbidden_call(self, forbidden_tools: set[str]) -> list[str]:
        """Return list of tool names that were called but shouldn't have been."""
        return [tc.name for tc in self.tool_calls if tc.name in forbidden_tools]

    def bash_commands(self) -> list[str]:
        """Return all bash command strings executed."""
        return [
            tc.args.get("command", "")
            for tc in self.calls_to("bash")
        ]


class ScriptedModel:
    """Fake model that returns predetermined responses from a queue.

    Each call to respond() pops the next response from the queue.
    When the queue is exhausted, returns a content response to end the loop.
    """

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self._responses = list(responses)
        self._call_count = 0
        self.call_history: list[list[dict]] = []

    def respond(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        """Return the next scripted response. Records message history."""
        self.call_history.append(list(messages))
        self._call_count += 1
        if self._responses:
            return self._responses.pop(0)
        # Default: return content to end the loop
        return {
            "choices": [{"message": {"role": "assistant", "content": "done"}}]
        }

    @property
    def call_count(self) -> int:
        return self._call_count


def tool_response(
    tool_name: str,
    arguments: dict[str, Any],
    call_id: str = "call_1",
) -> dict[str, Any]:
    """Build a model response containing a single tool_call."""
    return {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "arguments": json.dumps(arguments),
                    },
                }],
            }
        }]
    }


def multi_tool_response(
    calls: list[tuple[str, dict[str, Any]]],
    start_id: int = 1,
) -> dict[str, Any]:
    """Build a model response containing multiple parallel tool_calls."""
    tool_calls = []
    for i, (name, args) in enumerate(calls):
        tool_calls.append({
            "id": f"call_{start_id + i}",
            "type": "function",
            "function": {
                "name": name,
                "arguments": json.dumps(args),
            },
        })
    return {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": tool_calls,
            }
        }]
    }


def content_response(text: str) -> dict[str, Any]:
    """Build a model response containing only text content (ends the loop)."""
    return {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": text,
            }
        }]
    }


def empty_response() -> dict[str, Any]:
    """Build an empty model response (triggers nudge logic)."""
    return {"choices": [{"message": {"role": "assistant", "content": ""}}]}


def run_agent_loop(
    bot_name: str,
    mission_prompt: str,
    model: ScriptedModel,
    state_dir: Path,
    max_iterations: int = MAX_ITERATIONS,
) -> AgentResult:
    """Execute the agent tool loop with a scripted model.

    Replicates api_runner.py:1731-1818 without HTTP calls or sys.exit().
    All tool calls go through the real _execute_tool() pipeline.

    Args:
        bot_name: Agent identifier for logging/status.
        mission_prompt: Full role prompt text.
        model: ScriptedModel providing predetermined responses.
        state_dir: Temporary directory for heartbeat/checkpoint/state files.
        max_iterations: Safety limit on tool loop iterations.

    Returns:
        AgentResult with complete execution history.
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    messages: list[dict[str, Any]] = [{"role": "user", "content": mission_prompt}]
    tool_calls_log: list[ToolCallRecord] = []
    files_touched: list[str] = []
    tickets_created = 0
    tool_iterations = 0
    continue_nudges = 0
    exit_reason = "unknown"

    while tool_iterations < max_iterations:
        resp = model.respond(messages)
        choices = resp.get("choices") or []
        msg = choices[0].get("message", {}) if choices else {}
        tc_list = msg.get("tool_calls")
        content = msg.get("content")

        if tc_list:
            messages.append(msg)
            for tc in tc_list:
                tc_id = tc.get("id", "")
                func = tc.get("function", {}) or {}
                name = func.get("name", "")
                args_raw = func.get("arguments", "{}")

                if isinstance(args_raw, dict):
                    args = args_raw
                else:
                    try:
                        args = json.loads(args_raw) if args_raw else {}
                    except Exception:
                        args = {}
                    if not isinstance(args, dict):
                        args = {}

                result = _execute_tool(name, args)

                record = ToolCallRecord(
                    name=name,
                    args=args,
                    result=result,
                    iteration=tool_iterations,
                )
                tool_calls_log.append(record)

                file_path = args.get("filePath") or args.get("path")
                if name in ("edit", "write") and file_path:
                    files_touched.append(file_path)

                if name == "create_ticket" and result.get("success"):
                    tickets_created += 1

                tool_msg = {
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": json.dumps(result),
                }
                messages.append(tool_msg)

            tool_iterations += 1
            continue_nudges = 0
            continue

        if content and str(content).strip():
            exit_reason = "completed"
            break

        if continue_nudges < 2:
            messages.append({"role": "user", "content": "continue"})
            continue_nudges += 1
            continue

        exit_reason = "no_content"
        break
    else:
        exit_reason = "iteration_limit"

    return AgentResult(
        bot_name=bot_name,
        exit_reason=exit_reason,
        tool_calls=tool_calls_log,
        messages=messages,
        iterations=tool_iterations,
        tickets_created=tickets_created,
        files_touched=files_touched,
    )


def load_role_prompt(role_name: str) -> str:
    """Load a role prompt from codebot/roles/ by name."""
    roles_dir = Path(__file__).parent.parent / "codebot" / "roles"
    prompt_path = roles_dir / f"{role_name}.md"
    if not prompt_path.exists():
        raise FileNotFoundError(f"Role prompt not found: {prompt_path}")
    return prompt_path.read_text(encoding="utf-8")
