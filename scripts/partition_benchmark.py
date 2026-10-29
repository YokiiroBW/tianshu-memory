"""Fixed-size candidate-stage replay; excludes authority I/O and external models."""

import json
import statistics
import time

from tianshu_memory.memory_context.partition import interleave, partition
from tianshu_memory.partition_index import rank_terms


def main():
    result = []
    for owners in (10, 50, 200):
        entries = [
            (dict(id=f"p{i % owners}:{i}", scope=f"p{i % owners}"), [], []) for i in range(600)
        ]
        indexed = {row[0]["id"]: "meeting plan library river tomorrow" for row in entries}
        samples = []
        for _ in range(40):
            started = time.perf_counter()
            ranked = rank_terms(partition(entries), indexed, ["meeting", "library"])
            selected = interleave(ranked)
            samples.append((time.perf_counter() - started) * 1000)
        result.append(
            dict(
                owners=owners,
                documents=600,
                p50_ms=statistics.median(samples),
                p95_ms=sorted(samples)[37],
                distinct_subjects=len({row[0]["scope"] for row in selected[:owners]}),
            )
        )
    print(json.dumps(dict(boundary=__doc__, measurements=result, model_calls=0)))


if __name__ == "__main__":
    main()
