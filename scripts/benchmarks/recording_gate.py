"""Measure admission latency and Python heap separately; verify admitted work."""

import gc
import json
import statistics
import tempfile
import time
import tracemalloc
from pathlib import Path
from uuid import uuid4

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

from phoenix.server.recording import ConversationKey, RecordingGate


def measure(source, memory=False):
    owner = ConversationKey(source, str(uuid4()))
    with tempfile.TemporaryDirectory() as directory:
        gate = RecordingGate(ledger_path=Path(directory) / "ledger.db")
        capture = gate.start(owner, "bounded load measurement", str(uuid4()))
        batches = []
        for offset in range(0, 10000, 100):
            request = ExportTraceServiceRequest()
            resource = request.resource_spans.add()
            resource.resource.attributes.add(key="service.name").value.string_value = source
            group = resource.scope_spans.add()
            for index in range(offset, offset + 100):
                now = time.time_ns()
                span = group.spans.add(
                    trace_id=b"t" * 16,
                    span_id=(index + 1).to_bytes(8, "big"),
                    name="tool.operation",
                    start_time_unix_nano=now,
                    end_time_unix_nano=now + 1000,
                )
                key = "conversation.id" if source == "codex" else "session.id"
                span.attributes.add(key=key).value.string_value = owner.conversation_id
                if source == "kiro":
                    span.attributes.add(
                        key="recording.capture_id"
                    ).value.string_value = capture.capture_id
                    span.attributes.add(key="kiro.event_id").value.string_value = str(uuid4())
                span.attributes.add(key="input.value").value.string_value = "x" * 1024
            batches.append(request)
        gc.collect()
        if memory:
            tracemalloc.start()
        latencies = []
        admitted = 0
        try:
            for request in batches:
                started = time.perf_counter()
                result = gate.admit(request)
                latencies.append((time.perf_counter() - started) * 1000)
                admitted += sum(len(g.spans) for r in result.resource_spans for g in r.scope_spans)
            assert admitted == 10000, admitted
            assert gate.status(owner, capture.capture_id).queued_spans == 10000
            heap = tracemalloc.get_traced_memory() if memory else None
            return {
                "source": source,
                "admitted": admitted,
                "batch_size": 100,
                "latency_ms_p50": statistics.median(latencies),
                "latency_ms_p95": sorted(latencies)[94],
                "python_heap_bytes": heap,
            }
        finally:
            if memory:
                tracemalloc.stop()
            gate.close()


if __name__ == "__main__":
    results = [measure(source) for _ in range(5) for source in ("codex", "kiro")]
    results.extend(measure(source, memory=True) for source in ("codex", "kiro"))
    print(
        json.dumps(
            {
                "scope": (
                    "gate plus SQLite ledger; excludes HTTP, primary DB, "
                    "and protobuf native allocations from heap"
                ),
                "runs": results,
            },
            indent=2,
        )
    )
