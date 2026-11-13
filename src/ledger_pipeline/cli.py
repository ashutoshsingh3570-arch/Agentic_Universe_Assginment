"""Command-line interface for the Interaction Ledger pipeline."""
from __future__ import annotations

import argparse
import json
import sys
from .core import Ledger


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="ledger_pipeline",
        description="Ingest campaign lead/event drops and generate reports.",
    )
    parser.add_argument(
        "--db",
        default="ledger.db",
        metavar="PATH",
        help="SQLite database path (default: ledger.db)",
    )
    parser.add_argument(
        "--key-file",
        default=".ledger.key",
        metavar="PATH",
        help="Fernet encryption key file (default: .ledger.key). Keep it private.",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    # --- ingest ---
    ingest_p = sub.add_parser("ingest", help="Ingest one delivery drop directory")
    ingest_p.add_argument("drop", help="Path to a drop directory")
    ingest_p.add_argument(
        "--schema-dir",
        default=None,
        metavar="PATH",
        help="Directory containing workflow *.json schemas (default: ./schemas)",
    )
    ingest_p.add_argument(
        "--dispositions",
        default=None,
        metavar="PATH",
        help="Path to dispositions.csv (default: ./schemas/dispositions.csv)",
    )

    # --- report ---
    report_p = sub.add_parser("report", help="Generate the five contract report files")
    report_p.add_argument(
        "--out",
        default="reports",
        metavar="DIR",
        help="Output directory for report files (default: reports/)",
    )
    report_p.add_argument(
        "--dispositions",
        default=None,
        metavar="PATH",
        help="Path to dispositions.csv (default: ./schemas/dispositions.csv)",
    )

    args = parser.parse_args()

    ledger = Ledger(args.db, args.key_file)
    try:
        if args.command == "ingest":
            result = ledger.ingest(
                args.drop,
                schema_dir=args.schema_dir,
                dispositions_path=args.dispositions,
            )
        else:
            result = ledger.report(
                args.out,
                dispositions_path=args.dispositions,
            )
        print(json.dumps(result, indent=2))
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        ledger.close()


if __name__ == "__main__":
    main()
