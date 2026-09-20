"""Tests for web_tools, context_compactor, scratchpad, and task_splitter."""
import json
import pytest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from codebot.web_tools import web_search, web_fetch, is_blocked_url, extract_text_from_html
from codebot.context_compactor import (
    estimate_tokens, estimate_messages_tokens, needs_compaction,
    compact_messages, build_compaction_checkpoint, SUMMARY_MARKER,
)
from codebot.scratchpad import (
    ScratchpadState, load_scratchpad, save_scratchpad, clear_scratchpad,
    create_handoff_note, MAX_SCRATCHPAD_BYTES,
)
from codebot.task_splitter import should_split, split_ticket, compute_chunks


class TestIsBlockedUrl:
    def test_blocks_localhost(self):
        assert is_blocked_url("http://localhost/admin") is True

    def test_blocks_127(self):
        assert is_blocked_url("http://127.0.0.1:8080") is True

    def test_blocks_private_10(self):
        assert is_blocked_url("http://10.0.0.1/internal") is True

    def test_blocks_private_192(self):
        assert is_blocked_url("http://192.168.1.1/admin") is True

    def test_blocks_link_local(self):
        assert is_blocked_url("http://169.254.169.254/metadata") is True

    def test_allows_public(self):
        assert is_blocked_url("https://example.com/page") is False

    def test_allows_duckduckgo(self):
        assert is_blocked_url("https://html.duckduckgo.com/html/?q=test") is False

    def test_blocks_empty(self):
        assert is_blocked_url("") is True


class TestExtractTextFromHtml:
    def test_strips_tags(self):
        html = "<p>Hello <b>world</b></p>"
        text = _extract_text_from_html(html)
        assert "Hello" in text
        assert "world" in text
        assert "<p>" not in text

    def test_strips_script(self):
        html = "<p>Visible</p><script>alert('xss')</script>"
        text = _extract_text_from_html(html)
        assert "Visible" in text
        assert "alert" not in text

    def test_strips_style(self):
        html = "<p>Text</p><style>.x{color:red}</style>"
        text = _extract_text_from_html(html)
        assert "Text" in text
        assert "color" not in text


class TestWebSearch:
    def test_empty_query(self):
        result = web_search("")
        assert result["success"] is False
        assert "empty" in result["error"]

    def test_whitespace_query(self):
        result = web_search("   ")
        assert result["success"] is False

    def test_returns_dict_shape(self):
        result = web_search("test")
        assert "success" in result
        assert "output" in result
        assert "error" in result


class TestWebFetch:
    def test_empty_url(self):
        result = web_fetch("")
        assert result["success"] is False

    def test_blocks_non_http(self):
        result = web_fetch("ftp://example.com/file")
        assert result["success"] is False
        assert "http" in result["error"]

    def test_blocks_private_url(self):
        result = web_fetch("http://127.0.0.1/admin")
        assert result["success"] is False
        assert "blocked" in result["error"]

    def test_returns_dict_shape(self):
        result = web_fetch("https://example.com")
        assert "success" in result
        assert "output" in result
        assert "error" in result


class TestEstimateTokens:
    def test_basic(self):
        assert estimate_tokens("hello world") == 2

    def test_empty(self):
        assert estimate_tokens("") == 1

    def test_long_text(self):
        text = "a" * 4000
        assert estimate_tokens(text) == 1000


class TestEstimateMessagesTokens:
    def test_single_message(self):
        msgs = [{"role": "user", "content": "hello"}]
        tokens = estimate_messages_tokens(msgs)
        assert tokens > 0

    def test_multiple_messages(self):
        msgs = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello!"},
        ]
        tokens = estimate_messages_tokens(msgs)
        assert tokens > 5


class TestNeedsCompaction:
    def test_small_history_no_compaction(self):
        msgs = [{"role": "user", "content": "hi"}]
        assert needs_compaction(msgs, max_tokens=10000) is False

    def test_large_history_needs_compaction(self):
        msgs = [{"role": "user", "content": "x" * 40000}] * 5
        assert needs_compaction(msgs, max_tokens=10000) is True

    def test_early_exit_on_large_history(self):
        """Verify needs_compaction returns quickly for very large histories."""
        # Create a history that is way over the limit
        msgs = [{"role": "user", "content": "x" * 4000}] * 1000
        # Should return True without processing all messages if optimized
        assert needs_compaction(msgs, max_tokens=1000) is True


class TestCompactMessages:
    def test_short_history_unchanged(self):
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        result = compact_messages(msgs, max_tokens=100000)
        assert len(result) == 3

    def test_long_history_compacted(self):
        msgs = [{"role": "system", "content": "sys"}]
        for i in range(20):
            msgs.append({"role": "user", "content": f"msg {i} " + "x" * 4000})
            msgs.append({"role": "assistant", "content": f"reply {i} " + "x" * 4000})
        result = compact_messages(msgs, max_tokens=10000)
        assert len(result) < len(msgs)
        assert any(SUMMARY_MARKER in m.get("content", "") for m in result)
        assert result[0]["role"] == "system"

    def test_preserves_recent_messages(self):
        msgs = [{"role": "system", "content": "sys"}]
        for i in range(20):
            msgs.append({"role": "user", "content": f"msg {i} " + "x" * 4000})
        result = compact_messages(msgs, max_tokens=10000)
        last_content = result[-1]["content"]
        assert "msg 19" in last_content


class TestBuildCompactionCheckpoint:
    def test_structure(self):
        cp = build_compaction_checkpoint("CB-1", ["step1"], ["step2"], ["file.py"])
        assert cp["ticket_id"] == "CB-1"
        assert cp["completed_steps"] == ["step1"]
        assert cp["remaining_steps"] == ["step2"]


class TestScratchpadState:
    def test_defaults(self):
        s = ScratchpadState()
        assert s.phase == "init"
        assert s.completed_steps == []
        assert s.version == 2

    def test_mark_step_complete(self):
        s = ScratchpadState(remaining_steps=["a", "b", "c"])
        s.mark_step_complete("b")
        assert "b" in s.completed_steps
        assert "b" not in s.remaining_steps

    def test_mark_error(self):
        s = ScratchpadState()
        s.mark_error("something broke")
        assert s.phase == "error"
        assert s.error_message == "something broke"

    def test_serialization_roundtrip(self):
        s = ScratchpadState(ticket_id="CB-1", phase="running", iteration=5)
        raw = s.to_json()
        s2 = ScratchpadState.from_json(raw)
        assert s2.ticket_id == "CB-1"
        assert s2.phase == "running"
        assert s2.iteration == 5

    def test_truncate_for_size(self):
        s = ScratchpadState(context_summary="x" * 20000)
        s.truncate_for_size()
        assert len(s.to_json().encode("utf-8")) <= MAX_SCRATCHPAD_BYTES


class TestScratchpadPersistence:
    def test_save_and_load(self, tmp_path):
        s = ScratchpadState(ticket_id="CB-1", current_agent="worker-1", phase="running")
        save_scratchpad(tmp_path, s)
        loaded = load_scratchpad(tmp_path, "CB-1")
        assert loaded.ticket_id == "CB-1"
        assert loaded.phase == "running"

    def test_load_missing_returns_fresh(self, tmp_path):
        loaded = load_scratchpad(tmp_path, "nonexistent")
        assert loaded.phase == "init"
        assert loaded.ticket_id == "nonexistent"

    def test_clear(self, tmp_path):
        s = ScratchpadState(ticket_id="w1", current_agent="w1")
        save_scratchpad(tmp_path, s)
        clear_scratchpad(tmp_path, "w1")
        loaded = load_scratchpad(tmp_path, "w1")
        assert loaded.phase == "init"


class TestHandoffNote:
    def test_contains_key_info(self):
        s = ScratchpadState(
            ticket_id="CB-1", current_agent="worker-1", phase="error",
            completed_steps=["step1"], remaining_steps=["step2", "step3"],
            error_message="timeout",
        )
        note = create_handoff_note(s)
        assert "CB-1" in note
        assert "worker-1" in note
        assert "timeout" in note
        assert "step1" in note
        assert "step2" in note


class TestShouldSplit:
    def _make_ticket(self, **kwargs):
        from codebot.ticket_engine import create_ticket, TicketClass, Severity
        defaults = dict(
            title="Test", ticket_class=TicketClass.BUG, severity=Severity.MEDIUM,
            source="test", evidence="ev", problem_statement="prob",
            desired_state="des", acceptance_criteria=["ac"],
        )
        defaults.update(kwargs)
        return create_ticket(**defaults)

    def test_no_split_on_complete(self, tmp_path):
        from codebot.ticket_engine import TicketState, TicketStore, RiskLevel
        from codebot.implementation_planner import PlanStore
        t = self._make_ticket(risk=RiskLevel.LOW)
        store = TicketStore(tmp_path / "tickets.json")
        store.add(t)
        for s in [TicketState.VALIDATING, TicketState.TRIAGED, TicketState.READY,
                   TicketState.IMPLEMENTING, TicketState.REVIEWING, TicketState.VERIFYING, TicketState.COMPLETE]:
            if s == TicketState.COMPLETE:
                # need gate pass
                import json, os, time
                gate_path = tmp_path / "gate_results.jsonl"
                record = {"ticket_id": t.id, "passed": True, "timestamp": time.time(), "gates": []}
                gate_path.write_text(json.dumps(record)+"\n", encoding="utf-8")
            store.transition(t.id, s)
        t = store.get(t.id)
        assert should_split(t, exit_reason="timeout") is False

    def test_split_on_timeout(self):
        t = self._make_ticket()
        assert should_split(t, exit_reason="timeout") is True

    def test_split_on_rate_limit(self):
        t = self._make_ticket()
        assert should_split(t, exit_reason="rate_limit") is True

    def test_split_on_many_modules(self):
        t = self._make_ticket(affected_modules=["a.py", "b.py", "c.py", "d.py", "e.py"])
        assert should_split(t) is True

    def test_no_split_small_ticket(self):
        t = self._make_ticket(affected_modules=["a.py"])
        assert should_split(t) is False


class TestComputeChunks:
    def test_chunk_by_modules(self):
        from codebot.ticket_engine import create_ticket, TicketClass, Severity
        t = create_ticket("T", TicketClass.BUG, Severity.MEDIUM, "s", "e", "p", "d",
                          ["ac"], affected_modules=["a.py", "b.py", "c.py", "d.py", "e.py"])
        chunks = _compute_chunks(t, None)
        assert len(chunks) >= 2
        assert all("modules" in c for c in chunks)

    def test_max_chunks_capped(self):
        from codebot.ticket_engine import create_ticket, TicketClass, Severity
        modules = [f"file{i}.py" for i in range(50)]
        t = create_ticket("T", TicketClass.BUG, Severity.MEDIUM, "s", "e", "p", "d",
                          ["ac"], affected_modules=modules)
        chunks = _compute_chunks(t, None)
        assert len(chunks) <= 10
