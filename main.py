"""Command line entry for the invoice pipeline.

    python3 main.py --invoice_path=data/invoices/invoice_1001.txt
    python3 main.py --invoice_path=data/invoices --offline
    python3 main.py --batch=data/invoices --offline

A folder passed to --invoice_path is processed in filename order.
main.py creates inventory.db in the current directory when it is missing.
"""

import argparse
import json
import sys
from pathlib import Path

from db import create_database
from llm_client import GrokClient
from pipeline import invoice_files, process_many


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the invoice pipeline on one file or a folder.")
    parser.add_argument("--invoice_path", help="Invoice file, or a folder of invoices")
    parser.add_argument("--batch", help="Folder of invoices. Same result as passing that folder to --invoice_path")
    parser.add_argument("--offline", action="store_true", help="Force offline mode, even if XAI_API_KEY is set")
    parser.add_argument("--smoke", action="store_true", help="Send one short Grok request and print only status and latency")
    args = parser.parse_args(argv)

    client = GrokClient(offline=True if args.offline else None)
    if args.smoke:
        return _smoke(client)
    if args.invoice_path and args.batch:
        print("Pass either --invoice_path or --batch, not both.", file=sys.stderr)
        return 2
    raw = args.batch or args.invoice_path
    if not raw:
        parser.error("missing --invoice_path")

    path = Path(raw)
    if not path.exists():
        print(json.dumps({"error": "not found", "path": str(path)}), file=sys.stderr)
        return 2
    if args.batch and not path.is_dir():
        print(json.dumps({"error": "--batch needs a folder", "path": str(path)}), file=sys.stderr)
        return 2

    db_path = create_database("inventory.db")
    files = invoice_files(path) if path.is_dir() else [path]
    process_many(files, db_path, Path("logs"), client)
    return 0


def _smoke(client: GrokClient) -> int:
    if client.offline:
        print(json.dumps({"smoke": "skipped", "offline": True, "model": client.model}))
        return 0
    try:
        result = client.complete([{"role": "user", "content": "Reply with the single word pong."}])
    except Exception as exc:
        print(json.dumps({"smoke": "failed", "offline": False, "model": client.model, "error": str(exc)}))
        return 1
    print(json.dumps({
        "smoke": "ok",
        "offline": False,
        "model": result.model,
        "latency_ms": round(result.latency_ms, 1),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
