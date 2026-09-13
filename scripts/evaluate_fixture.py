"""Annotated lexical retrieval cases, including adverse neighbors and constrained budgets."""

import argparse
import copy
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from tianshu_memory.app import configured_app
from tianshu_memory.domain import canonical, now, utc
from tianshu_memory.workflow import LocalWorkflow


def unit(statement, conditions=(), negations=()):
    return {
        "statement": statement,
        "conditions": list(conditions),
        "negations": list(negations),
        "valid_time": "current",
        "uncertainty": "confirmed",
        "reality": "real",
    }


COFFEE = unit("晚上不喝咖啡，白天偶尔可以", ["白天偶尔可以"], ["晚上不喝咖啡"])


def evaluate(config, requests, runtime):
    # Repeat evaluations never add neighbors to the supplied fixture database.
    config = copy.deepcopy(config)
    with closing(
        sqlite3.connect(f"file:{Path(config['database_path']).as_posix()}?mode=ro", uri=True)
    ) as original:
        with closing(sqlite3.connect(runtime / "evaluation.sqlite")) as copied:
            original.backup(copied)
    config["database_path"] = str(runtime / "evaluation.sqlite")
    config_path = runtime / "config.json"
    config_path.write_text(canonical(config), encoding="utf-8")
    os.environ["TIANSHU_MEMORY_CONFIG"] = str(config_path)
    app = configured_app()
    service, evidence = app.state.memory, []
    worker = LocalWorkflow(service)
    worker.rebuild_index()

    def corpus(label, groups):
        scope = dict(requests["private"]["requested_scope"], conversation_id=f"evaluation-{label}")
        context = copy.deepcopy(config["origins"]["origin-private"])
        context.update(assertion_ref=f"origin-{label}", allowed_scope=scope)
        config["origins"][f"origin-{label}"] = context
        config_path.write_text(canonical(config), encoding="utf-8")
        source = {
            "message_key": {
                "channel": context["verified_channel"],
                "message_id": f"evaluation-{label}",
                "revision": 1,
            },
            "receipt_id": f"receipt-{label}",
            "archive_state": "pending",
            "locator": None,
        }
        worker.observe_source(source, scope)
        event = {
            "schema_version": 1,
            "event_id": f"event-{label}",
            "event_type": "conversation.turn_committed",
            "owner": "companion",
            "aggregate_id": f"turn-{label}",
            "aggregate_version": 1,
            "occurred_at": utc(now()),
            "causation_id": f"request-{label}",
            "conversation_id": scope["conversation_id"],
            "turn_sequence": 1,
            "scope": scope,
            "scope_version": 1,
            "input_revision": 1,
            "sources": [source],
            "reality": "real",
            "confirmed_user_correction": False,
            "delivery_state": "not_required",
            "reply_ids": [],
        }
        job = service.consume(
            event, {"authenticated_service": "companion", "allowed_scopes": [scope]}
        )
        worker.commit_candidate(
            job["candidate_job_ref"],
            [
                {
                    "scope": scope,
                    "category": "evidence",
                    "field_key": key,
                    "units": [dict(item, sources=[source]) for item in annotations],
                }
                for key, annotations in groups
            ],
        )
        request = copy.deepcopy(requests["private"])
        request["requested_scope"] = scope
        request["query"]["origin"]["assertion_ref"] = f"origin-{label}"
        return request

    with TestClient(app) as client:

        def ask(request, query, budget):
            payload = copy.deepcopy(request)
            payload.update(query_text=query, budget={"tokens": budget, "bytes": budget})
            response = client.post(
                "/internal/v1/memory/select",
                json=payload,
                headers={"Authorization": "Bearer " + config["callers"]["companion"]["token"]},
            )
            response.raise_for_status()
            return response.json()

        def check(label, request, query, budget, required):
            result = ask(request, query, budget)
            selected = result["selected_units"]
            wanted = {u["statement"]: u for u in required}
            covered = {u["statement"] for u in selected}.intersection(wanted)
            irrelevant = sum(u["statement"] not in wanted for u in selected)
            faithful = all(
                u["subject_person_id"] == request["requested_scope"]["person_id"]
                and all(u[key] == value for key, value in wanted[u["statement"]].items())
                for u in selected
                if u["statement"] in wanted
            )
            group = request["requested_scope"]["audience"] == "group"
            leaked = group and any(
                word in canonical(result)
                for word in ["message_key", "receipt_id", "archive_state", "locator"]
            )
            assert (
                len(selected) == len(required) and len(covered) == len(required) and not irrelevant
            )
            assert faithful and not leaked
            evidence.append(
                {
                    "sample": label,
                    "query": query,
                    "required_statements": list(wanted),
                    "required_units": len(required),
                    "selected_units": len(selected),
                    "required_evidence_coverage": len(covered) / len(required)
                    if required
                    else None,
                    "irrelevant_units": irrelevant,
                    "subject_condition_negation_preserved": faithful,
                    "private_source_leaks": int(leaked),
                    "request_byte_cap": budget,
                    "injection_utf8_bytes": result["budget_used"]["bytes"],
                    "token_estimate": result["budget_used"]["tokens"],
                }
            )

        for label, scope, query, budget, required in [
            ("private-full-clause", "private", "咖啡", 4096, [COFFEE]),
            ("group-projection", "group", "咖啡", 4096, [COFFEE]),
            ("exact-field", "private", "field:coffee", 4096, [COFFEE]),
            ("greeting-zero-history", "private", "你好", 4096, []),
            ("unrelated-topic", "private", "音乐", 4096, []),
            ("zero-budget", "private", "咖啡", 0, []),
        ]:
            check(label, requests[scope], query, budget, required)
        for index, query in enumerate(
            [
                "今天天气怎么样",
                "晚上天气怎么样",
                "今天穿什么好",
                "白天穿什么",
                "晚上好呀",
                "今天过得怎么样",
                "晚上吃什么",
                "现在可以聊聊吗",
                "晚上有什么电影推荐",
            ]
        ):
            check(f"common-context-negative-{index + 1}", requests["private"], query, 4096, [])

        temporal = corpus("temporal", [("movie", [unit("晚上看电影")]), ("coffee", [COFFEE])])
        budget = ask(temporal, "field:coffee", 4096)["budget_used"]["bytes"]
        check("topic-over-time-one-group-budget", temporal, "晚上咖啡", budget, [COFFEE])

        flavour_units = [
            unit("咖啡口味偏清淡", ["口味适用于白天"]),
            unit("晚上不喝咖啡", negations=["晚上不喝咖啡"]),
        ]
        flavour = corpus(
            "flavour", [("cup", [unit("咖啡杯颜色是蓝色")]), ("flavour", flavour_units)]
        )
        budget = ask(flavour, "field:flavour", 4096)["budget_used"]["bytes"]
        check(
            "coverage-rank-with-complete-dependency-budget",
            flavour,
            "咖啡口味",
            budget,
            flavour_units,
        )

        focused_unit = unit("喜欢咖啡")
        focused = corpus(
            "focused",
            [
                (
                    "many-interests",
                    [unit("喜欢咖啡，也喜欢音乐电影摄影阅读旅行绘画游泳跑步烹饪园艺")],
                ),
                ("coffee", [focused_unit]),
            ],
        )
        budget = ask(focused, "field:coffee", 4096)["budget_used"]["bytes"]
        check("fts-score-on-equal-topic-coverage", focused, "咖啡", budget, [focused_unit])
    return {
        "scope": "18 annotated lexical cases, including adverse neighbors and budgets; no embedding or semantic benchmark",
        "samples": evidence,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", required=True)
    args = parser.parse_args()
    directory = Path(args.fixture).resolve()
    config = json.loads((directory / "config.json").read_text(encoding="utf-8"))
    if config["mode"] != "local_fixture":
        parser.error("Only explicit synthetic fixtures are permitted")
    requests = {
        label: json.loads((directory / f"select-{label}.json").read_text(encoding="utf-8"))
        for label in ["private", "group"]
    }
    with TemporaryDirectory(prefix="evaluation-", dir=directory) as temporary:
        runtime = Path(temporary).resolve()
        assert runtime.is_relative_to(
            directory
        )  # Verify cleanup target stays inside named fixture.
        report = evaluate(config, requests, runtime)
    (directory / "evaluation.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
