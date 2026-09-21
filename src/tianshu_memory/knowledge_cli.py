"""Explicit local operator and stdio client entrypoints, separate from chat auth."""

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

from .diagnostics import KNOWLEDGE_SERVICE
from .domain import Fault, strict_json
from .knowledge import KnowledgeApplication
from .knowledge_catalog_migration import migrate as migrate_catalog
from .knowledge_directories_migration import migrate as migrate_directories
from .knowledge_migration import migrate
from .lessons_migration import migrate as migrate_lessons
from .research_notes_migration import migrate as migrate_research_notes
from .server_runtime import add_serve_arguments
from .store import Store


def main():
    parser = argparse.ArgumentParser(description="Explicit project knowledge operations")
    parser.add_argument("--config", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    migration = commands.add_parser("migrate")
    migration.add_argument("--backup", required=True)
    upgrade = commands.add_parser("migrate-lessons")
    upgrade.add_argument("--backup", required=True)
    plans = commands.add_parser("migrate-directories")
    plans.add_argument("--backup", required=True)
    notes = commands.add_parser("migrate-research-notes")
    notes.add_argument("--backup", required=True)
    # The document catalogue needs its own explicit upgrade: two indexes and a version row, on a
    # database that is stopped first and backed up whole. No request path ever runs this.
    catalog = commands.add_parser("migrate-catalog")
    catalog.add_argument("--backup", required=True)
    for name in ("action", "mcp"):
        command = commands.add_parser(name)
        command.add_argument("--client", required=True)
        command.add_argument("--credential-env", required=True)
        if name == "action":
            command.add_argument("file")
    # The restricted HTTP entry: one fixed client, one explicit port, and the shared deployment
    # options. The credential is whatever each request presents as `Authorization: Bearer`,
    # exactly as `action` reads it from the environment and hands it to the same `execute`; a
    # request body is never allowed to name an identity.
    serve = commands.add_parser("serve")
    serve.add_argument("--client", required=True)
    serve.add_argument("--port", type=int, required=True)
    add_serve_arguments(serve)
    args = parser.parse_args()
    try:
        if args.command in {
            "migrate",
            "migrate-lessons",
            "migrate-directories",
            "migrate-research-notes",
            "migrate-catalog",
        }:
            config = strict_json(Path(args.config).read_bytes())
            store = Store(
                config["database_path"],
                recovery_path=config.get("source_sync", {}).get("recovery_path"),
            )
            result = {
                "migrate": migrate,
                "migrate-lessons": migrate_lessons,
                "migrate-directories": migrate_directories,
                "migrate-research-notes": migrate_research_notes,
                "migrate-catalog": migrate_catalog,
            }[args.command](store, args.backup)
        elif args.command == "mcp":
            from .knowledge_mcp import create_server

            create_server(args.config, args.client, args.credential_env).run(transport="stdio")
            return
        elif args.command == "serve":
            from .knowledge_http import create_app, probe_handles
            from .runtime_probes import ProbeConfig
            from .server_runtime import resolve_binding, serve

            # No credential is configured here: every request presents its own `Authorization:
            # Bearer` value and the existing `knowledge.clients` decides whether it authorizes
            # anything. A process that cannot read its configuration or whose named client is not
            # registered refuses to start, so it can never look healthy and then refuse every
            # request. The deployment options are the shared ones; the default binding stays
            # loopback.
            binding = resolve_binding(
                host=args.host,
                port=args.port,
                certfile=args.tls_certfile,
                keyfile=args.tls_keyfile,
                allowed_hosts=args.allowed_host or [],
            )

            def build():
                # The deployment's own validated authorities go to the business entry as well, so
                # the inner Host check accepts exactly what this binding accepted. Without them a
                # legal non-loopback deployment would pass the outer check and then be refused by
                # the entry's own loopback rule — reachable as a probe and unusable as a service.
                # This hands over the names that were already validated before the socket existed;
                # it does not relax the check, derive anything from a request or accept a wildcard.
                return create_app(
                    args.config,
                    args.client,
                    args.port,
                    authorities=binding.authority_names(),
                )

            def probe_settings(assembly):
                return ProbeConfig(
                    service=KNOWLEDGE_SERVICE,
                    diagnostics=assembly.diagnostics,
                    config_path=args.config,
                    contract_path=assembly.diagnostics.contract_path,
                    runtime=assembly,
                    client=args.client,
                    handles=lambda: probe_handles(assembly.app, args.port),
                )

            return serve(
                KNOWLEDGE_SERVICE,
                build,
                args=args,
                binding=binding,
                probe_settings=probe_settings,
            )
        else:
            with Path(args.file).open("rb") as stream:
                raw = stream.read(262145)
            if len(raw) > 262144:
                raise ValueError("Operation too large")
            result = KnowledgeApplication(args.config).execute(
                strict_json(raw), client=args.client, credential=os.environ.get(args.credential_env)
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        # A partial directory apply is not success: some confirmed items were refused.
        if result.get("status") in {"failed", "partial"}:
            raise SystemExit(1)
    except (Fault, OSError, sqlite3.Error, ValueError, KeyError, TypeError, ImportError) as error:
        code = error.code if isinstance(error, Fault) else "dependency_or_input_error"
        print(
            json.dumps({"status": "failed", "code": code}),
            file=sys.stderr if args.command == "mcp" else sys.stdout,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
