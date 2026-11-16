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
        self.assertEqual(result.coverage["import_schema_version"], 2)
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


if __name__ == "__main__":
    unittest.main()
