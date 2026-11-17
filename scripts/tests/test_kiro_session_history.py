import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

MODULE = Path(__file__).resolve().parents[2] / "src/phoenix/server/kiro_session_history.py"
spec = importlib.util.spec_from_file_location("kiro_session_history_test_target", MODULE)
history = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = history
spec.loader.exec_module(history)

SESSION = "sess_019a74e9-a998-7320-abcd-012345678901"
SUB = "6f1c2d3e-4a5b-4c6d-8e7f-0123456789ab"
SUB2 = "7a2d3e4f-5b6c-4d7e-9f80-123456789abc"


def row(ident, kind, second=0, **payload):
    return {
        "id": ident,
        "timestamp": f"2026-11-15T18:00:{second:02d}.000Z",
        "payload": {"type": kind, **payload},
    }


def attrs(span):
    return {item.key: item.value.string_value for item in span.attributes}


class SessionHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory = self.root / ("a" * 16) / SESSION
        self.directory.mkdir(parents=True)
        (self.directory / "session.json").write_text(
            json.dumps({"id": SESSION, "title": "Fixture Session"}), encoding="utf-8"
        )

    def write(self, rows, suffix=""):
        (self.directory / "messages.jsonl").write_text(
            "".join(json.dumps(item) + "\n" for item in rows) + suffix, encoding="utf-8"
        )

    def build(self):
        return history.build_import(history.load_snapshot(SESSION, sessions_root=self.root))

    def spans(self, result):
        return [s for r in result.request.resource_spans for g in r.scope_spans for s in g.spans]

    def fixture(self):
        return [
            row("u", "user", content="List workspace"),
            row("start", "turn_start", 1, turnId="turn-1", boundarySource="observed"),
            row("secret", "assistant", 2, operationType="Reasoning", content="PRIVATE_REASONING"),
            row("call", "tool_call", 3, turnId="turn-1", toolCallId="tool-1", toolName="execute_pwsh", title="List workspace", args={"command": "ls"}, status="completed"),
            row("result", "tool_result", 3, turnId="turn-1", toolCallId="tool-1", content="Output:\nExit Code: 0", success=True),
            row("say", "assistant", 4, turnId="turn-1", operationType="Say", content="Finished"),
            row("end", "turn_end", 5, turnId="turn-1", boundarySource="observed"),
        ]

    def test_tools_are_one_step_and_completed_snapshots_do_not_invent_duration(self):
        self.write(self.fixture())
        result = self.build()
        tools = [s for s in self.spans(result) if attrs(s).get("sessionlog.kind") == "tool"]
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0].start_time_unix_nano, tools[0].end_time_unix_nano)
        self.assertEqual(json.loads(attrs(tools[0])["sessionlog.source_record_ids"]), ["call", "result"])
        self.assertEqual(attrs(tools[0])["kiro.result"], "success")
        self.assertNotIn(b"PRIVATE_REASONING", result.request.SerializeToString())
        self.assertEqual(result.coverage["excluded_by_type"]["assistant.Reasoning"], 1)
        self.assertEqual(result.coverage["visible_steps"], 5)

    def test_snapshot_identity_and_span_keys_are_idempotent(self):
        self.write(self.fixture())
        first, second = self.build(), self.build()
        self.assertEqual(first.import_id, second.import_id)
        self.assertEqual(first.expected_span_keys, second.expected_span_keys)
        self.assertEqual(first.request.SerializeToString(), second.request.SerializeToString())
        self.write(self.fixture() + [row("next", "user", 6, content="Next")])
        self.assertNotEqual(first.import_id, self.build().import_id)

    def test_all_visible_control_and_permission_steps_are_retained(self):
        rows = [row("user", "user", content="Record")]
        rows += [row("call-" + name, "tool_call", 1, toolCallId=name, toolName="execute_pwsh", title=name) for name in ["START", "STOP", "STATUS"]]
        rows += [row("result-" + name, "tool_result", 2, toolCallId=name, success=True, content="done") for name in ["START", "STOP", "STATUS"]]
        rows += [row("p", "pending_interaction", 3, toolCallId="STATUS", interactionType="tool_approval", question="Allow?"), row("r", "interaction_resolved", 4, toolCallId="STATUS", outcome="selected"), row("hook", "ContextualHookInvoked", 5, name="recorder", status="completed", content="DO_NOT_COPY_HOOK"), row("say", "assistant", 6, operationType="Say", content="Final reply")]
        self.write(rows)
        result = self.build()
        kinds = [attrs(s).get("sessionlog.kind") for s in self.spans(result)]
        self.assertEqual(kinds.count("tool"), 3)
        self.assertIn("pending_interaction", kinds)
        self.assertIn("interaction_resolved", kinds)
        self.assertIn("ContextualHookInvoked", kinds)
        self.assertNotIn(b"DO_NOT_COPY_HOOK", result.request.SerializeToString())

    def test_open_turn_and_orphan_result_are_not_reported_complete(self):
        self.write([row("start", "turn_start", turnId="open", boundarySource="observed"), row("result", "tool_result", 1, toolCallId="missing", success=True, content="done")])
        result = self.build()
        self.assertIn("open_turn", result.gaps)
        self.assertIn("orphan_tool_result", result.gaps)

    def test_partial_and_malformed_rows_are_visible_in_coverage(self):
        self.write([row("u", "user", content="hello")], suffix='invalid\n{"unfinished":')
        result = self.build()
        self.assertIn("malformed_row", result.gaps)
        self.assertIn("unfinished_row", result.gaps)

    def test_unknown_type_and_missing_timestamp_cannot_silently_pass(self):
        missing = row("bad", "user", content="hello")
        missing.pop("timestamp")
        self.write([row("u", "user", content="hello"), row("x", "new_event"), missing])
        result = self.build()
        self.assertIn("unknown_event_type", result.gaps)
        self.assertIn("invalid_timestamp", result.gaps)

    def test_conflicting_duplicate_record_id_is_rejected(self):
        self.write([row("same", "user", content="one"), row("same", "user", content="two")])
        with self.assertRaises(ValueError):
            self.build()

    def test_metadata_mismatch_and_invalid_identity_are_rejected(self):
        self.write(self.fixture())
        with self.assertRaises(ValueError):
            history.load_snapshot("../escape", sessions_root=self.root)
        (self.directory / "session.json").write_text('{"id":"other"}', encoding="utf-8")
        with self.assertRaises(ValueError):
            self.build()

    def test_transcript_link_escape_is_rejected(self):
        outside = self.root / "outside.jsonl"
        outside.write_text(json.dumps(row("u", "user", content="hello")) + "\n", encoding="utf-8")
        try:
            os.symlink(outside, self.directory / "messages.jsonl")
        except OSError:
            self.skipTest("Symlinks are unavailable on this host")
        with self.assertRaises(ValueError):
            self.build()

    def test_sensitive_text_is_redacted_and_truncation_is_reported(self):
        self.write([row("u", "user", content="Authorization: Bearer TOP_SECRET\npassword=hunter2\n" + "x" * 20000)])
        result = self.build()
        raw = result.request.SerializeToString()
        self.assertNotIn(b"TOP_SECRET", raw)
        self.assertNotIn(b"hunter2", raw)
        self.assertIn("content_truncated", result.gaps)

    def test_structured_tool_arguments_do_not_leak_credentials(self):
        self.write([
            row("call", "tool_call", toolCallId="one", toolName="fixture", args={"password": "HIDDEN_PASSWORD", "headers": {"Authorization": "Bearer HIDDEN_AUTH"}, "nested": {"api_key": "HIDDEN_API_KEY"}}),
            row("result", "tool_result", 1, toolCallId="one", content="done", success=True),
        ])
        raw = self.build().request.SerializeToString()
        for secret in [b"HIDDEN_PASSWORD", b"HIDDEN_AUTH", b"HIDDEN_API_KEY"]:
            self.assertNotIn(secret, raw)

    def test_shell_exit_code_overrides_native_tool_completion(self):
        for tool in ["execute_pwsh", "execute_bash"]:
            with self.subTest(tool=tool):
                self.write([
                    row("call", "tool_call", toolCallId="one", toolName=tool),
                    row("result", "tool_result", 1, toolCallId="one", content="Output:\nExit Code: 7", success=True),
                ])
                operation = next(s for s in self.spans(self.build()) if attrs(s).get("sessionlog.kind") == "tool")
                self.assertEqual(attrs(operation)["kiro.result"], "error")
                self.assertEqual(attrs(operation)["kiro.exit_code"], "7")
                self.assertEqual(operation.status.code, 2)

    def test_shell_without_exit_code_is_unknown_and_non_shell_is_tool_level(self):
        for tool, expected in [("execute_pwsh", "unknown"), ("load_resource", "success")]:
            with self.subTest(tool=tool):
                self.write([
                    row("call", "tool_call", toolCallId="one", toolName=tool),
                    row("result", "tool_result", 1, toolCallId="one", content="finished", success=True),
                ])
                operation = next(s for s in self.spans(self.build()) if attrs(s).get("sessionlog.kind") == "tool")
                self.assertEqual(attrs(operation)["kiro.result"], expected)
                self.assertIn("not business outcome", attrs(operation)["sessionlog.result_semantics"])

    def test_delimiter_bearing_source_ids_cannot_collide(self):
        self.write([
            row("x", "tool_call", toolCallId="one", toolName="fixture"),
            row("y:z", "tool_result", 1, toolCallId="one", success=True, content="one"),
            row("x:y", "tool_call", 2, toolCallId="two", toolName="fixture"),
            row("z", "tool_result", 3, toolCallId="two", success=True, content="two"),
        ])
        result = self.build()
        self.assertEqual(len(result.expected_span_keys), len(self.spans(result)))
        tools = [s for s in self.spans(result) if attrs(s).get("sessionlog.kind") == "tool"]
        self.assertNotEqual(tools[0].span_id, tools[1].span_id)

    def test_import_identity_includes_the_builder_schema_version(self):
        from uuid import NAMESPACE_URL, uuid5

        self.write(self.fixture())
        snapshot = history.load_snapshot(SESSION, sessions_root=self.root)
        result = history.build_import(snapshot)
        old_id = str(uuid5(NAMESPACE_URL, "kiro-session-history:" + SESSION + ":" + snapshot.sha256))
        self.assertNotEqual(result.import_id, old_id)
        self.assertEqual(result.coverage["import_schema_version"], 3)
        self.assertEqual(result.import_id, self.build().import_id)

    def test_completed_turn_prefix_is_stable_when_later_turn_is_appended(self):
        self.write(self.fixture())
        first = history.load_snapshot(SESSION, sessions_root=self.root, through_turn_id="turn-1")
        self.write(self.fixture() + [
            row("later-user", "user", 6, content="LATER_TURN_SECRET"),
            row("later-start", "turn_start", 7, turnId="turn-2", boundarySource="observed"),
            row("later-say", "assistant", 8, turnId="turn-2", operationType="Say", content="LATER_REPLY_SECRET"),
            row("later-end", "turn_end", 9, turnId="turn-2", boundarySource="observed"),
        ])
        second = history.load_snapshot(SESSION, sessions_root=self.root, through_turn_id="turn-1")
        self.assertEqual(first.sha256, second.sha256)
        self.assertEqual(first.row_count, second.row_count)
        self.assertEqual(second.through_record_id, "end")
        first_import, second_import = history.build_import(first), history.build_import(second)
        self.assertEqual(first_import.import_id, second_import.import_id)
        self.assertEqual(first_import.expected_span_keys, second_import.expected_span_keys)
        self.assertNotIn(b"LATER_TURN_SECRET", second_import.request.SerializeToString())
        self.assertNotIn(b"LATER_REPLY_SECRET", second_import.request.SerializeToString())

    def test_requested_turn_requires_an_observed_completion_boundary(self):
        self.write(self.fixture()[:-1])
        with self.assertRaises(ValueError):
            history.load_snapshot(SESSION, sessions_root=self.root, through_turn_id="turn-1")
        rows = self.fixture()
        rows[-1]["payload"]["boundarySource"] = "inferred"
        self.write(rows)
        with self.assertRaises(ValueError):
            history.load_snapshot(SESSION, sessions_root=self.root, through_turn_id="turn-1")

    def test_execution_id_can_identify_the_requested_turn_boundary(self):
        rows = self.fixture()
        rows[-1]["payload"].pop("turnId")
        rows[-1]["payload"]["executionId"] = "turn-1"
        self.write(rows)
        snapshot = history.load_snapshot(SESSION, sessions_root=self.root, through_turn_id="turn-1")
        self.assertEqual(snapshot.through_record_id, "end")

    # --- sub-agent runs -------------------------------------------------

    def write_sub(self, rows, sub=None, suffix=""):
        folder = self.directory / "sub-executions"
        folder.mkdir(exist_ok=True)
        (folder / f"{sub or SUB}.jsonl").write_text(
            "".join(json.dumps(item) + "\n" for item in rows) + suffix, encoding="utf-8"
        )

    def sub_rows(self):
        extra = {"subExecutionId": SUB, "executionId": "sub-exec-1"}
        return [
            row("s-hook", "ContextualHookInvoked", 3, name="sub-hook", status="completed", **extra),
            row("s-call", "tool_call", 4, toolCallId="tool-1", toolName="load_resource", title="Sub tool", args={"id": "x"}, **extra),
            row("s-result", "tool_result", 4, toolCallId="tool-1", content="sub done", success=True, **extra),
            row("s-think", "assistant", 5, operationType="Reasoning", content="SUB_PRIVATE_REASONING", **extra),
            row("s-steer", "steering_inclusion", 5, content="SUB_STEERING", **extra),
            row("s-say", "assistant", 5, operationType="Say", content="sub finished", **extra),
        ]

    def sub_fixture(self, start=True, complete=True, parent="turn-1"):
        rows = [row("u", "user", content="Review"), row("start", "turn_start", 1, turnId="turn-1", boundarySource="observed")]
        if start:
            rows.append(row("sa-start", "sub_agent_start", 2, parentExecutionId=parent, subSessionId=SUB, subAgentName="reviewer", prompt="SUB_PROMPT password=hunter2", explanation="Why delegate"))
        if complete:
            rows.append(row("sa-done", "sub_agent_complete", 7, parentExecutionId=parent, subSessionId=SUB, response="SUB_RESPONSE", status="success"))
        rows.append(row("end", "turn_end", 8, turnId="turn-1", boundarySource="observed"))
        return rows

    def kinds(self, result, kind):
        return [s for s in self.spans(result) if attrs(s).get("sessionlog.kind") == kind]

    def test_paired_sub_agent_becomes_one_span_under_its_turn(self):
        self.write(self.sub_fixture())
        self.write_sub(self.sub_rows())
        result = self.build()
        (agent,) = self.kinds(result, "sub_agent")
        (turn,) = self.kinds(result, "turn")
        self.assertEqual(agent.name, "kiro.sub_agent")
        self.assertEqual(agent.parent_span_id, turn.span_id)
        self.assertLess(agent.start_time_unix_nano, agent.end_time_unix_nano)
        values = attrs(agent)
        self.assertEqual(values["kiro.sub_agent.name"], "reviewer")
        self.assertEqual(values["kiro.sub_agent.explanation"], "Why delegate")
        self.assertEqual(values["kiro.sub_agent.status"], "success")
        self.assertEqual(values["kiro.result"], "success")
        self.assertEqual(values["output.value"], "SUB_RESPONSE")
        self.assertIn("SUB_PROMPT", values["input.value"])
        self.assertNotIn("hunter2", values["input.value"])
        self.assertEqual(agent.status.code, 1)
        self.assertNotIn("unknown_event_type", result.gaps)
        self.assertNotIn("unpaired_sub_agent", result.gaps)
        self.assertNotIn("sub_execution_missing", result.gaps)
        self.assertEqual(result.coverage["included_by_type"]["sub_agent_start"], 1)
        self.assertEqual(result.coverage["included_by_type"]["sub_agent_complete"], 1)
        self.assertNotIn("sub_agent_start", result.coverage["excluded_by_type"])

    def test_sub_execution_rows_nest_under_the_sub_agent_span(self):
        self.write(self.sub_fixture())
        self.write_sub(self.sub_rows())
        result = self.build()
        (agent,) = self.kinds(result, "sub_agent")
        children = [s for s in self.spans(result) if s.parent_span_id == agent.span_id]
        self.assertEqual(sorted(attrs(s)["sessionlog.kind"] for s in children), ["ContextualHookInvoked", "assistant", "tool"])
        (tool,) = [s for s in children if attrs(s)["sessionlog.kind"] == "tool"]
        self.assertEqual(attrs(tool)["kiro.result"], "success")
        self.assertEqual(json.loads(attrs(tool)["sessionlog.source_record_ids"]), ["s-call", "s-result"])
        raw = result.request.SerializeToString()
        self.assertNotIn(b"SUB_PRIVATE_REASONING", raw)
        self.assertNotIn(b"SUB_STEERING", raw)
        self.assertEqual(result.coverage["excluded_by_type"]["steering_inclusion"], 1)
        self.assertEqual(result.coverage["excluded_by_type"]["assistant.Reasoning"], 1)
        self.assertEqual(len(result.expected_span_keys), len(self.spans(result)))
        order = [int(attrs(s)["sessionlog.order"]) for s in children]
        self.assertEqual(order, sorted(order))

    def test_sub_run_tool_ids_do_not_clash_with_main_transcript_tool_ids(self):
        rows = self.sub_fixture()
        rows[2:2] = [
            row("m-call", "tool_call", 1, turnId="turn-1", toolCallId="tool-1", toolName="fixture"),
            row("m-result", "tool_result", 1, turnId="turn-1", toolCallId="tool-1", content="main", success=True),
        ]
        self.write(rows)
        self.write_sub(self.sub_rows())
        result = self.build()
        self.assertEqual(len(self.kinds(result, "tool")), 2)
        self.assertEqual(len(result.expected_span_keys), len(self.spans(result)))

    def test_missing_sub_execution_file_is_a_visible_gap(self):
        self.write(self.sub_fixture())
        result = self.build()
        self.assertIn("sub_execution_missing", result.gaps)
        self.assertEqual(len(self.kinds(result, "sub_agent")), 1)
        self.assertFalse(result.coverage["complete"])

    def test_unpaired_start_is_never_reported_complete(self):
        self.write(self.sub_fixture(complete=False))
        self.write_sub(self.sub_rows())
        result = self.build()
        self.assertIn("unpaired_sub_agent", result.gaps)
        (agent,) = self.kinds(result, "sub_agent")
        self.assertEqual(agent.end_time_unix_nano, self.ns(5))  # latest child step; no complete record
        self.assertEqual(attrs(agent)["kiro.result"], "incomplete")
        self.assertEqual(agent.status.code, 0)
        self.assertNotIn("output.value", attrs(agent))

    def test_unpaired_complete_is_a_gap(self):
        self.write(self.sub_fixture(start=False))
        self.write_sub(self.sub_rows())
        result = self.build()
        self.assertIn("unpaired_sub_agent", result.gaps)
        (agent,) = self.kinds(result, "sub_agent")
        self.assertEqual(attrs(agent)["output.value"], "SUB_RESPONSE")

    def test_unknown_parent_falls_back_to_the_current_turn(self):
        self.write(self.sub_fixture(parent="no-such-turn"))
        self.write_sub(self.sub_rows())
        result = self.build()
        (agent,) = self.kinds(result, "sub_agent")
        (turn,) = self.kinds(result, "turn")
        self.assertEqual(agent.parent_span_id, turn.span_id)

    def test_invalid_or_escaping_sub_session_ids_are_never_used_as_paths(self):
        outside = self.directory / "secret.jsonl"
        outside.write_text(json.dumps(row("leak", "assistant", 3, operationType="Say", content="ESCAPED_CONTENT")) + "\n", encoding="utf-8")
        for bad in ["../secret", "..\\secret", "not-a-uuid", SUB.upper(), SUB + "/x"]:
            with self.subTest(bad=bad):
                rows = self.sub_fixture()
                for item in rows:
                    if "subSessionId" in item["payload"]:
                        item["payload"]["subSessionId"] = bad
                self.write(rows)
                result = self.build()
                self.assertIn("sub_execution_missing", result.gaps)
                self.assertNotIn(b"ESCAPED_CONTENT", result.request.SerializeToString())

    def test_symlinked_sub_execution_file_is_refused(self):
        self.write(self.sub_fixture())
        outside = self.root / "outside.jsonl"
        outside.write_text("".join(json.dumps(item) + "\n" for item in self.sub_rows()), encoding="utf-8")
        (self.directory / "sub-executions").mkdir()
        try:
            os.symlink(outside, self.directory / "sub-executions" / f"{SUB}.jsonl")
        except OSError:
            self.skipTest("Symlinks are unavailable on this host")
        result = self.build()
        self.assertIn("sub_execution_missing", result.gaps)
        self.assertEqual([s for s in self.spans(result) if attrs(s).get("sessionlog.kind") == "tool"], [])

    def test_sub_execution_malformed_rows_use_existing_gap_semantics(self):
        self.write(self.sub_fixture())
        self.write_sub(self.sub_rows(), suffix='garbage\n{"unfinished":')
        result = self.build()
        self.assertIn("malformed_row", result.gaps)
        self.assertIn("unfinished_row", result.gaps)
        self.assertNotIn("sub_execution_missing", result.gaps)

    def test_genuinely_unknown_event_type_is_still_reported_with_sub_agents(self):
        rows = self.sub_fixture()
        rows.insert(3, row("x", "new_event", 2))
        self.write(rows)
        self.write_sub(self.sub_rows())
        self.assertIn("unknown_event_type", self.build().gaps)
        self.write(self.sub_fixture())
        self.write_sub(self.sub_rows() + [row("s-x", "new_event", 5, subExecutionId=SUB)])
        self.assertIn("unknown_event_type", self.build().gaps)

    def test_sub_agent_after_the_turn_boundary_is_excluded(self):
        later = SUB2
        rows = self.sub_fixture() + [
            row("t2", "turn_start", 9, turnId="turn-2", boundarySource="observed"),
            row("sa2-start", "sub_agent_start", 10, parentExecutionId="turn-2", subSessionId=later, subAgentName="later", prompt="LATER_PROMPT", explanation="x"),
            row("sa2-done", "sub_agent_complete", 11, parentExecutionId="turn-2", subSessionId=later, response="LATER_RESPONSE", status="success"),
            row("t2-end", "turn_end", 12, turnId="turn-2", boundarySource="observed"),
        ]
        self.write(rows)
        self.write_sub(self.sub_rows())
        self.write_sub([row("l-say", "assistant", 10, operationType="Say", content="LATER_SUB_CONTENT", subExecutionId=later)], sub=later)
        cut = history.load_snapshot(SESSION, sessions_root=self.root, through_turn_id="turn-1")
        result = history.build_import(cut)
        raw = result.request.SerializeToString()
        self.assertEqual(len(self.kinds(result, "sub_agent")), 1)
        for text in [b"LATER_PROMPT", b"LATER_RESPONSE", b"LATER_SUB_CONTENT"]:
            self.assertNotIn(text, raw)
        self.assertEqual(result.gaps, ())
        full = self.build()
        self.assertEqual(len(self.kinds(full, "sub_agent")), 2)

    def test_snapshot_hash_covers_sub_execution_content(self):
        self.write(self.sub_fixture())
        self.write_sub(self.sub_rows())
        first = history.load_snapshot(SESSION, sessions_root=self.root)
        again = history.load_snapshot(SESSION, sessions_root=self.root)
        self.assertEqual(first.sha256, again.sha256)
        changed = self.sub_rows()
        changed[-1]["payload"]["content"] = "edited"
        self.write_sub(changed)
        second = history.load_snapshot(SESSION, sessions_root=self.root)
        self.assertNotEqual(first.sha256, second.sha256)
        self.assertNotEqual(history.build_import(first).import_id, history.build_import(second).import_id)

    def test_sub_agent_import_is_deterministic(self):
        self.write(self.sub_fixture())
        self.write_sub(self.sub_rows())
        first, second = self.build(), self.build()
        self.assertEqual(first.import_id, second.import_id)
        self.assertEqual(first.expected_span_keys, second.expected_span_keys)
        self.assertEqual(first.request.SerializeToString(), second.request.SerializeToString())

    def test_sub_execution_files_count_toward_the_row_and_byte_limits(self):
        self.write(self.sub_fixture())
        self.write_sub(self.sub_rows())
        original = history.MAX_ROWS
        self.addCleanup(setattr, history, "MAX_ROWS", original)
        history.MAX_ROWS = len(self.sub_fixture()) + len(self.sub_rows()) - 1
        with self.assertRaises(ValueError):
            self.build()
        history.MAX_ROWS = original
        size = (self.directory / "messages.jsonl").stat().st_size
        original_bytes = history.MAX_BYTES
        self.addCleanup(setattr, history, "MAX_BYTES", original_bytes)
        history.MAX_BYTES = size + 10
        with self.assertRaises(ValueError):
            self.build()

    def test_sub_agent_spans_count_toward_the_span_limit(self):
        self.write(self.sub_fixture())
        self.write_sub(self.sub_rows())
        original = history.MAX_SPANS
        self.addCleanup(setattr, history, "MAX_SPANS", original)
        history.MAX_SPANS = 5
        with self.assertRaises(ValueError):
            self.build()


    # --- time containment ------------------------------------------------

    @staticmethod
    def ns(second):
        from datetime import datetime, timezone

        return int(datetime(2026, 11, 15, 18, 0, second, tzinfo=timezone.utc).timestamp()) * 1_000_000_000

    def assert_contained(self, result):
        by_id = {s.span_id: s for s in self.spans(result)}
        for span in by_id.values():
            if not span.parent_span_id:
                continue
            parent = by_id[span.parent_span_id]
            kind = attrs(span)["sessionlog.kind"]
            self.assertLessEqual(parent.start_time_unix_nano, span.start_time_unix_nano, kind)
            self.assertGreaterEqual(parent.end_time_unix_nano, span.end_time_unix_nano, kind)

    def test_sub_agent_span_contains_children_outside_its_start_and_complete(self):
        self.write(self.sub_fixture())
        extra = {"subExecutionId": SUB, "executionId": "sub-exec-1"}
        self.write_sub([
            row("early", "assistant", 0, operationType="Say", content="early", **extra),
            row("late", "assistant", 9, operationType="Say", content="late", **extra),
        ])
        result = self.build()
        (agent,) = self.kinds(result, "sub_agent")
        (turn,) = self.kinds(result, "turn")
        self.assertEqual((agent.start_time_unix_nano, agent.end_time_unix_nano), (self.ns(0), self.ns(9)))
        self.assertEqual((turn.start_time_unix_nano, turn.end_time_unix_nano), (self.ns(0), self.ns(9)))
        children = sorted(s.start_time_unix_nano for s in self.spans(result) if s.parent_span_id == agent.span_id)
        self.assertEqual(children, [self.ns(0), self.ns(9)])
        self.assert_contained(result)

    def test_running_sub_agent_ends_at_its_latest_child(self):
        self.write(self.sub_fixture(complete=False))
        self.write_sub(self.sub_rows())
        result = self.build()
        (agent,) = self.kinds(result, "sub_agent")
        self.assertEqual((agent.start_time_unix_nano, agent.end_time_unix_nano), (self.ns(2), self.ns(5)))
        self.assertEqual(attrs(agent)["kiro.result"], "incomplete")
        self.assert_contained(result)

    def test_running_sub_agent_without_children_stays_a_point(self):
        self.write(self.sub_fixture(complete=False))
        result = self.build()
        (agent,) = self.kinds(result, "sub_agent")
        self.assertEqual(agent.start_time_unix_nano, agent.end_time_unix_nano)
        self.assert_contained(result)

    def test_unfinished_turn_and_envelope_contain_late_sub_agent_children(self):
        self.write(self.sub_fixture()[:-1])
        self.write_sub([row("late", "assistant", 12, operationType="Say", content="late", subExecutionId=SUB)])
        result = self.build()
        (turn,) = self.kinds(result, "turn")
        self.assertGreaterEqual(turn.end_time_unix_nano, self.ns(12))
        self.assert_contained(result)


if __name__ == "__main__":
    unittest.main()
