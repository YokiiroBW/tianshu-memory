import argparse
import json
import os
import sqlite3
from pathlib import Path

from jsonschema.exceptions import ValidationError

from .app import configured_app, runtime_app
from .diagnostics import CHAT_SERVICE
from .domain import Fault, strict_json
from .server_runtime import add_serve_arguments, serve
from .store import Store
from .user_actions import LocalUserApplication
from .workflow import LocalWorkflow


def serve_deployment(args):
    """Run the deployment `serve` command through the shared runtime assembly.

    The deployment options are the shared ones: an IP-literal `--host`, an explicit port, and the
    TLS and authority arguments a non-loopback bind requires. The application is the same
    `runtime_app` composition the product already had, built here so the runtime can install the
    diagnostic stack, the two probes and the authority check around it.
    """
    from .runtime_probes import ProbeConfig

    def build():
        return runtime_app(args.config, args.diagnostics_contract)

    def probe_settings(assembly):
        return ProbeConfig(
            service=CHAT_SERVICE,
            diagnostics=assembly.diagnostics,
            config_path=args.config,
            contract_path=assembly.diagnostics.contract_path,
            runtime=assembly,
            handles=lambda: assembly.app.state.memory is not None,
        )

    return serve(CHAT_SERVICE, build, args=args, probe_settings=probe_settings)


def main():
    parser = argparse.ArgumentParser(
        description="Local memory service and isolated fixture workflow"
    )
    parser.add_argument("--config", required=True, help="Explicit private runtime JSON config")
    sub = parser.add_subparsers(dest="operation", required=True)
    serve_parser = sub.add_parser(
        "serve", help="One HTTP process: identity, chat, profile and source-sync entry"
    )
    serve_parser.add_argument("--port", type=int, default=8130)
    add_serve_arguments(serve_parser)
    action = sub.add_parser(
        "fixture-action", help="Local synthetic source/review input; never production confirmation"
    )
    action.add_argument("file", help="JSON with operation and arguments, see docs/runtime.md")
    user = sub.add_parser("user-action", help="Execute the complete reviewed local user operation")
    user.add_argument("file", help="Full operation JSON; execution is explicit approval")
    user.add_argument("--principal", required=True)
    user.add_argument(
        "--credential-env",
        required=True,
        help="Environment variable holding the independent user credential",
    )
    migrate_users = sub.add_parser(
        "migrate-users", help="Stop writers; add guarded local approvals with backup"
    )
    migrate_users.add_argument("--backup", required=True)
    migrate_qq = sub.add_parser(
        "migrate-qq-aliases", help="Stop writers; add guarded QQ display names with backup"
    )
    migrate_qq.add_argument("--backup", required=True)
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
    migrate_lookup = sub.add_parser(
        "migrate-source-lookup", help="Stop writers; index guarded source receipts with backup"
    )
    migrate_lookup.add_argument("--backup", required=True)
    migrate_observations = sub.add_parser(
        "migrate-observations", help="Stop writers; add guarded passive sources with backup"
    )
    migrate_observations.add_argument("--backup", required=True)
    args = parser.parse_args()
    if args.operation == "serve":
        return serve_deployment(args)
    return run_local_operation(args, parser)


def run_local_operation(args, parser):
    """Every non-deployment operation, exactly as the product already ran it."""
    if args.operation in {
        "migrate-profiles",
        "migrate-sources",
        "migrate-users",
        "migrate-qq-aliases",
        "migrate-observations",
        "migrate-source-lookup",
    }:
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
        elif args.operation == "migrate-observations":
            result = store.migrate_observations(args.backup)
        elif args.operation == "migrate-source-lookup":
            result = store.migrate_source_lookup(args.backup)
        elif args.operation == "migrate-users":
            result = store.migrate_users(args.backup)
        elif args.operation == "migrate-qq-aliases":
            result = store.migrate_qq_aliases(args.backup)
        else:
            result = store.migrate_profiles(args.backup)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    os.environ["TIANSHU_MEMORY_CONFIG"] = args.config
    app = configured_app()
    if args.operation == "user-action":
        try:
            raw = Path(args.file).read_bytes()
            if len(raw) > 262144:
                raise ValueError("Operation too large")
            result = LocalUserApplication(app.state.memory, args.config).execute(
                strict_json(raw),
                principal=args.principal,
                credential=os.environ.get(args.credential_env),
            )
        except Fault as error:
            print(json.dumps({"code": error.code, "status": error.status}))
            raise SystemExit(1) from None
        except (ValidationError, ValueError, KeyError, TypeError):
            print(json.dumps({"code": "invalid_input", "status": 400}))
            raise SystemExit(1) from None
        except (OSError, sqlite3.Error):
            print(json.dumps({"code": "dependency_unavailable", "status": 503}))
            raise SystemExit(1) from None
        print(json.dumps(result, ensure_ascii=False, indent=2))
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


if __name__ == "__main__":
    # The console script installed from `[project.scripts]` calls `main` directly; this guard is
    # what makes `python -m tianshu_memory.cli` the same command, which is how the deployment
    # documentation and the process tests start it.
    main()
