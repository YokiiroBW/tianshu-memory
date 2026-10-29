"""Run synthetic keyword recall against an explicit answer set; no external model calls.

Run with the same contract environment as pytest:
  python tests/evaluate_recall.py --output .runtime/recall-quality.json
PYTHONPATH may point to a pinned baseline's src for a controlled before/after comparison.
"""

import argparse
import hashlib
import json
import tempfile
import time
from pathlib import Path

from conftest import Harness, contract_directory

from tianshu_memory.contracts import Contracts


def evaluate(corpus, *, preserve_order=False):
    with tempfile.TemporaryDirectory(prefix="tianshu-recall-eval-") as temporary:
        h = Harness(Path(temporary), Contracts(contract_directory()))
        try:
            h.seed(
                [
                    h.draft(
                        field_key=fact["id"],
                        units=[h.unit(statement=fact["text"], conditions=[], negations=[])],
                    )
                    for fact in corpus["facts"]
                ]
            )
            identities = {fact["text"]: fact["id"] for fact in corpus["facts"]}
            cases = []
            for case in corpus["queries"]:
                started = time.perf_counter()
                result = h.select(query=case["query"])
                elapsed = time.perf_counter() - started
                selected = {identities[unit["statement"]] for unit in result["selected_units"]}
                expected = set(case["expected"])
                cases.append(
                    dict(
                        **case,
                        selected=sorted(selected),
                        missing=sorted(expected - selected),
                        irrelevant=sorted(selected - expected),
                        exact=selected == expected,
                        elapsed_ms=elapsed * 1000,
                    )
                )
                if preserve_order:
                    cases[-1]["ranked"] = [
                        identities[unit["statement"]] for unit in result["selected_units"]
                    ]
        finally:
            h.client.close()
    summaries = {}
    for split in ["all", *sorted({case["split"] for case in cases})]:
        rows = [case for case in cases if split == "all" or case["split"] == split]
        correct = sum(len(set(row["selected"]).intersection(row["expected"])) for row in rows)
        selected = sum(len(row["selected"]) for row in rows)
        expected = sum(len(row["expected"]) for row in rows)
        unknown = [row for row in rows if not row["expected"]]
        summaries[split] = dict(
            cases=len(rows),
            exact_pass=sum(row["exact"] for row in rows),
            expected_units=expected,
            selected_units=selected,
            correct_units=correct,
            irrelevant_units=selected - correct,
            missing_units=expected - correct,
            precision=correct / selected if selected else None,
            recall=correct / expected if expected else None,
            unknown_cases=len(unknown),
            correct_abstentions=sum(not row["selected"] for row in unknown),
        )
    return dict(
        summaries=summaries,
        cases=cases,
        model_calls=0,
        boundary="Synthetic LocalWorkflow seed and native keyword select; not an LLM answer or production benchmark.",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--corpus", type=Path, default=Path(__file__).parent / "fixtures/recall_quality.json"
    )
    args = parser.parse_args()
    raw = args.corpus.read_bytes()
    report = evaluate(json.loads(raw))
    report["corpus_sha256"] = hashlib.sha256(raw).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["summaries"], ensure_ascii=False))
