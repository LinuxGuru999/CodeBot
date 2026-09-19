"""Agent testing harness for CodeBot role prompts.

Provides ScriptedModel for deterministic agent testing and
helper functions to load role prompts and run agent loops.
"""
import json
import sys
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

from codebot.api_runner import run_agent_loop as _api_run_agent_loop, AgentResult

# Re-export for convenience
__all__ = [
    "AgentResult",
    "ScriptedModel",
    "ToolCallRecord",
    "load_role_prompt",
    "run_agent_test",
    "run_agent_loop",
    "tool_response",
    "multi_tool_response",
    "content_response",
    "empty_response",
]


@dataclass
class ToolCallRecord:
    """Single tool invocation captured during agent execution (legacy compat)."""
    name: str
    args: dict[str, Any]
    result: dict[str, Any]
    iteration: int


class ScriptedModel:
    """Returns predetermined responses for agent loop testing.

    Supports both spec-style construction (no args + add_* methods) and
    legacy-style construction (list of response dicts).

    Usage (spec):
        model = ScriptedModel()
        model.add_tool_response("read", {"path": "/some/file"})
        model.add_content_response("All done")
        result = run_agent_loop("test_bot", "mission", model.respond, tmp_path)

    Usage (legacy):
        model = ScriptedModel([tool_response("read", {...}), content_response("done")])
        result = run_agent_loop("test_bot", "mission", model, tmp_path)
    """

    def __init__(self, responses: list[dict[str, Any]] | None = None):
        if responses is None:
            self._responses: list[dict] = []
        else:
            self._responses = list(responses)
        self._call_index = 0
        self._call_count = 0
        self.call_history: list[list[dict]] = []

    def add_tool_response(self, tool_name: str, arguments: dict, call_id: str = "call_1"):
        """Queue a response where the model requests a tool call."""
        self._responses.append({
            "choices": [{"message": {
                "tool_calls": [{
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "arguments": json.dumps(arguments)
                    }
                }],
                "content": None
            }}]
        })

    def add_multi_tool_response(self, tools: list[tuple[str, dict]], base_id: str = "call"):
        """Queue a response with multiple tool calls."""
        tool_calls = []
        for i, (name, args) in enumerate(tools):
            tool_calls.append({
                "id": f"{base_id}_{i}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)}
            })
        self._responses.append({
            "choices": [{"message": {"tool_calls": tool_calls, "content": None}}]
        })

    def add_content_response(self, text: str):
        """Queue a response where the model returns text (ends the loop)."""
        self._responses.append({
            "choices": [{"message": {"tool_calls": None, "content": text}}]
        })

    def add_empty_response(self):
        """Queue an empty response (triggers nudge)."""
        self._responses.append({
            "choices": [{"message": {"tool_calls": None, "content": None}}]
        })

    def respond(self, messages: list[dict]) -> dict:
        """Callable interface for run_agent_loop's model_responder parameter."""
        self.call_history.append(list(messages))
        self._call_count += 1
        if self._call_index < len(self._responses):
            resp = self._responses[self._call_index]
            self._call_index += 1
            return resp
        if self._responses and self._call_index >= len(self._responses):
            # legacy list was passed and we exhausted it via pop-style but using index
            # fallback to content to end loop
            return {"choices": [{"message": {"tool_calls": None, "content": "done"}}]}
        # no queued responses, end loop
        return {"choices": [{"message": {"tool_calls": None, "content": "done"}}]}

    @property
    def call_count(self) -> int:
        return self._call_count


def tool_response(
    tool_name: str,
    arguments: dict[str, Any],
    call_id: str = "call_1",
) -> dict[str, Any]:
    """Build a model response containing a single tool_call (legacy helper)."""
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
    """Build a model response containing multiple parallel tool_calls (legacy helper)."""
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


def load_role_prompt(role_name: str) -> str:
    """Load a role prompt from codebot/roles/{role_name}.md."""
    roles_dir = Path(__file__).parent.parent / "codebot" / "roles"
    prompt_path = roles_dir / f"{role_name}.md"
    if not prompt_path.exists():
        raise FileNotFoundError(f"Role prompt not found: {prompt_path}")
    return prompt_path.read_text(encoding="utf-8")


def run_agent_test(role_name: str, model: ScriptedModel, tmp_state_dir: Path, extra_context: str = "", max_iterations: int = 50) -> AgentResult:
    """Run an agent loop with a role prompt and scripted model.

    Args:
        role_name: Name of the role (e.g., "feature_hunter")
        model: ScriptedModel with queued responses
        tmp_state_dir: Temporary directory for state files
        extra_context: Additional context appended to the mission prompt
        max_iterations: Maximum tool loop iterations

    Returns:
        AgentResult with captured tool calls, tickets, etc.
    """
    prompt = load_role_prompt(role_name)
    mission = prompt
    if extra_context:
        mission = prompt + "\n\n--- Additional Context ---\n" + extra_context
    return _api_run_agent_loop(
        bot_name=role_name,
        mission_prompt=mission,
        model_responder=model.respond,
        state_dir=tmp_state_dir,
        max_iterations=max_iterations,
    )


def run_agent_loop(
    bot_name: str,
    mission_prompt: str,
    model: Any,
    state_dir: Path,
    max_iterations: int = 50,
) -> AgentResult:
    """Harness wrapper that delegates to api_runner.run_agent_loop.

    Accepts either a ScriptedModel instance or a callable responder.
    Legacy tests pass the model object; spec tests pass model.respond.
    """
    responder = model
    # api_runner handles both, but we normalize to ensure legacy ToolCallRecord compat is not needed
    return _api_run_agent_loop(
        bot_name=bot_name,
        mission_prompt=mission_prompt,
        model_responder=responder,
        state_dir=Path(state_dir),
        max_iterations=max_iterations,
    )
