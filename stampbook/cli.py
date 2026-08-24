from __future__ import annotations

import argparse
import json
from pathlib import Path

from .core import init_project, load_config, process_pending, recover_interrupted, scan_sources


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description="Build and review travel rubber stamps")
    command.add_argument("--config", type=Path, default=Path("config.json"))
    subcommands = command.add_subparsers(dest="command", required=True)
    subcommands.add_parser("init", help="Create folders and database")
    subcommands.add_parser("scan", help="Inventory source photos")
    process = subcommands.add_parser("process", help="Process pending photos")
    process.add_argument("--limit", type=int, default=15)
    process.add_argument("--dry-run", action="store_true", help="Validate inputs without API calls")
    serve = subcommands.add_parser("serve", help="Open the local review desk")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=7331)
    return command


def main() -> None:
    args = parser().parse_args()
    config = load_config(args.config)
    init_project(config)
    recovered = recover_interrupted(config)
    if args.command == "init":
        print(f"Project ready. Recovered {recovered} interrupted job(s).")
    elif args.command == "scan":
        print(json.dumps(scan_sources(config), indent=2))
    elif args.command == "process":
        if args.limit < 1:
            raise SystemExit("--limit must be at least 1")
        print(json.dumps(process_pending(config, args.limit, dry_run=args.dry_run), indent=2))
    elif args.command == "serve":
        from .web import create_app

        create_app(config).run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
