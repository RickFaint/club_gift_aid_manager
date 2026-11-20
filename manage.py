"""Deployment-safe database migration commands that do not import the Flask app."""

import argparse

from club_gift_aid_manager.db import DB_PATH
from club_gift_aid_manager.migrations import (
    current_heads,
    expected_heads,
    stamp_database_head,
    upgrade_database,
    verify_database_revision,
)


def main() -> None:
    """Run an explicit upgrade, verification, status, or legacy-adoption command."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("db-upgrade", "db-verify", "db-current", "db-stamp-head")
    )
    args = parser.parse_args()
    if args.command == "db-upgrade":
        upgrade_database(DB_PATH)
        verify_database_revision(DB_PATH)
        print(f"Database upgraded to {', '.join(sorted(expected_heads(DB_PATH)))}.")
    elif args.command == "db-verify":
        verify_database_revision(DB_PATH)
        print("Database revision and physical schema checks passed.")
    elif args.command == "db-stamp-head":
        stamp_database_head(DB_PATH)
        print("Existing schema stamped at the current revision; no migration DDL was run.")
    else:
        current = current_heads(DB_PATH)
        expected = expected_heads(DB_PATH)
        print(f"Current: {', '.join(sorted(current)) if current else 'unversioned'}")
        print(f"Expected: {', '.join(sorted(expected))}")


if __name__ == "__main__":
    main()
