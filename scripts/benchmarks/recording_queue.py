"""Measure the real writer admission budget without starting its database worker."""

import gc
import json
import tracemalloc
from datetime import datetime, timezone
from queue import SimpleQueue
from time import perf_counter

from phoenix.db.bulk_inserter import BulkInserter
from phoenix.trace.schemas import Span, SpanContext, SpanKind, SpanStatusCode


def measure(payload_bytes):
    gc.collect()
    now = datetime.now(timezone.utc)

    def offered():
        for index in range(12000):
            yield (
                Span(
                    name="queue measurement",
                    context=SpanContext(trace_id=f"{index:032x}", span_id=f"{index:016x}"),
                    span_kind=SpanKind.TOOL,
                    parent_id=None,
                    start_time=now,
                    end_time=now,
                    status_code=SpanStatusCode.OK,
                    status_message="",
                    events=[],
                    conversation=None,
                    attributes={
                        "recording.capture_id": "measurement",
                        "input.value": "x" * payload_bytes,
                    },
                ),
                "task-recordings",
            )

    tracemalloc.start()
    started = perf_counter()
    writer = BulkInserter(
        None, event_queue=SimpleQueue(), span_cost_calculator=None, initial_batch_of_spans=offered()
    )
    elapsed = perf_counter() - started
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    accepted = len(writer._spans)
    estimated = writer._managed_span_retained_bytes
    assert 0 < accepted <= 10000
    assert estimated <= 64 * 1024 * 1024
    assert current <= 64 * 1024 * 1024, current
    return {
        "offered": 12000,
        "accepted": accepted,
        "rejected": 12000 - accepted,
        "payload_bytes": payload_bytes,
        "estimated_retained_bytes": estimated,
        "python_heap_current_bytes": current,
        "python_heap_peak_bytes": peak,
        "seconds_with_tracemalloc": elapsed,
    }


if __name__ == "__main__":
    results = [measure(size) for _ in range(5) for size in (1024, 16384)]
    print(
        json.dumps(
            {
                "scope": (
                    "writer queue admission; Python allocations only; "
                    "excludes service baseline and native heap"
                ),
                "runs": results,
            },
            indent=2,
        )
    )
