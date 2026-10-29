"""Offline evidence replay. No model calls or writes to a supplied snapshot."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from evaluate_recall import evaluate  # noqa: E402


def metrics(cases, k):
    correct = total = complete = 0
    oracle = []
    for case in cases:
        expected, selected = set(case["expected"]), set(case["selected"][:k])
        correct += len(expected & selected)
        total += len(expected)
        complete += expected <= selected
        # Accuracy requires independently labelled oracle answers, not recall-as-accuracy.
        if type(case.get("oracle_correct")) is bool:
            oracle.append(case["oracle_correct"])
    return {
        "evidence_recall@k": correct / total if total else None,
        "complete_set_recall": complete / len(cases) if cases else None,
        "oracle_accuracy": sum(oracle) / len(oracle) if oracle else None,
        "oracle_labelled_cases": len(oracle),
        "k": k,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, help="JSON export containing synthetic facts")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--k", type=int, default=8)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    corpus = json.loads(args.fixture.read_text(encoding="utf-8"))
    if args.snapshot:
        corpus["facts"] = json.loads(args.snapshot.read_text(encoding="utf-8"))["facts"]
    report = evaluate(corpus)
    report["metrics"] = metrics(report["cases"], args.k)
    report["oracle_boundary"] = "Labelled oracle answers only; null means not evaluated."
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"metrics": report["metrics"], "model_calls": report["model_calls"]}))


if __name__ == "__main__":
    main()
