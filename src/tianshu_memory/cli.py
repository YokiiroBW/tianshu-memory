import argparse
import json
import os
from pathlib import Path

import uvicorn

from .app import configured_app
from .store import Store
from .workflow import LocalWorkflow


def main():
    parser = argparse.ArgumentParser(
        description="Local memory service and isolated fixture workflow"
    )
    parser.add_argument("--config", required=True, help="Explicit private runtime JSON config")
    sub = parser.add_subparsers(dest="operation", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--port", type=int, default=8130)
    action = sub.add_parser(
        "fixture-action", help="Local synthetic source/review input; never production confirmation"
    )
    action.add_argument("file", help="JSON with operation and arguments, see docs/runtime.md")
    sub.add_parser("jobs")
    sub.add_parser("rebuild-index")
    sub.add_parser("outbox")
    migrate = sub.add_parser(
        "migrate-profiles", help="Stop writers; explicitly migrate schema 1 to 2 with a backup"
    )
    migrate.add_argument("--backup", required=True, help="New local backup path; never overwritten")
    migrate_sources = sub.add_parser(
        "migrate-sources", help="Stop writers; migrate schema 2 to 3 with a backup"
    )
    migrate_sources.add_argument("--backup", required=True)
    args = parser.parse_args()
    if args.operation in {"migrate-profiles", "migrate-sources"}:
        config = json.loads(Path(args.config).read_text(encoding="utf-8"))
        store = Store(
            config["database_path"],
            recovery_path=config.get("source_sync", {}).get("recovery_path"),
        )
        if args.operation == "migrate-sources":
            from .contracts import Contracts

            contracts = Contracts(config["contract_directory"])
            contracts.load_sources()
            result = store.migrate_sources(args.backup, contracts)
        else:
            result = store.migrate_profiles(args.backup)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    os.environ["TIANSHU_MEMORY_CONFIG"] = args.config
    app = configured_app()
    if args.operation == "serve":
        uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False)
        return
    workflow = LocalWorkflow(app.state.memory)
    if args.operation == "fixture-action":
        data = json.loads(Path(args.file).read_text(encoding="utf-8"))
        allowed = {
            "observe_source",
            "confirm_revision",
            "commit_candidate",
            "acknowledge",
            "approve_profile",
            "publish_profile",
        }
        if data["operation"] not in allowed:
            parser.error("Unsupported fixture operation")
        result = getattr(workflow, data["operation"])(**data["arguments"])
    else:
        result = getattr(workflow, args.operation.replace("-", "_"))()
    print(json.dumps(result, ensure_ascii=False, indent=2))
