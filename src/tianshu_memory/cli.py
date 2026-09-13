import argparse
import json
import os

import uvicorn

from .app import configured_app
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
    args = parser.parse_args()
    os.environ["TIANSHU_MEMORY_CONFIG"] = args.config
    app = configured_app()
    if args.operation == "serve":
        uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False)
        return
    workflow = LocalWorkflow(app.state.memory)
    if args.operation == "fixture-action":
        from pathlib import Path

        data = json.loads(Path(args.file).read_text(encoding="utf-8"))
        allowed = {"observe_source", "confirm_revision", "commit_candidate", "acknowledge"}
        if data["operation"] not in allowed:
            parser.error("Unsupported fixture operation")
        result = getattr(workflow, data["operation"])(**data["arguments"])
    else:
        result = getattr(workflow, args.operation.replace("-", "_"))()
    print(json.dumps(result, ensure_ascii=False, indent=2))
