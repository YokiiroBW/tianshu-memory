"""Small human-labelled local retrieval fixture; not a semantic retrieval benchmark."""

import argparse
import copy
import json
import os
from pathlib import Path

from fastapi.testclient import TestClient

from tianshu_memory.app import configured_app
from tianshu_memory.domain import canonical


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", required=True)
    args = parser.parse_args()
    directory = Path(args.fixture).resolve()
    config = json.loads((directory / "config.json").read_text(encoding="utf-8"))
    if config["mode"] != "local_fixture":
        parser.error("Only explicit synthetic fixtures are permitted")
    os.environ["TIANSHU_MEMORY_CONFIG"] = str(directory / "config.json")
    requests = {
        label: json.loads((directory / f"select-{label}.json").read_text(encoding="utf-8"))
        for label in ["private", "group"]
    }
    samples = [
        ("private-full-clause", "private", "咖啡", 4096, 1),
        ("group-projection", "group", "咖啡", 4096, 1),
        ("exact-field", "private", "field:coffee", 4096, 1),
        ("greeting-zero-history", "private", "你好", 4096, 0),
        ("unrelated-topic", "private", "音乐", 4096, 0),
        ("zero-budget", "private", "咖啡", 0, 0),
    ]
    evidence = []
    with TestClient(configured_app()) as client:
        for label, scope, query, budget, expected in samples:
            request = copy.deepcopy(requests[scope])
            request.update(query_text=query, budget={"tokens": budget, "bytes": budget})
            response = client.post(
                "/internal/v1/memory/select",
                json=request,
                headers={"Authorization": "Bearer " + config["callers"]["companion"]["token"]},
            )
            response.raise_for_status()
            result = response.json()
            selected = result["selected_units"]
            assert len(selected) == expected
            faithful = all(
                u["conditions"] == ["白天偶尔可以"]
                and u["negations"] == ["晚上不喝咖啡"]
                and u["subject_person_id"] == request["requested_scope"]["person_id"]
                for u in selected
            )
            assert faithful
            leaked = scope == "group" and any(
                word in canonical(result)
                for word in ["message_key", "receipt_id", "archive_state", "locator"]
            )
            assert not leaked
            evidence.append(
                {
                    "sample": label,
                    "required_units": expected,
                    "selected_units": len(selected),
                    "required_evidence_coverage": 1.0 if expected else None,
                    "irrelevant_units": 0,
                    "subject_condition_negation_preserved": faithful,
                    "private_source_leaks": 0,
                    "injection_utf8_bytes": result["budget_used"]["bytes"],
                    "token_estimate": result["budget_used"]["tokens"],
                }
            )
    report = {
        "scope": "six annotated synthetic queries; no embedding/semantic benchmark",
        "samples": evidence,
    }
    (directory / "evaluation.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
