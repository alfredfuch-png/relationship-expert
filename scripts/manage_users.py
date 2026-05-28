#!/usr/bin/env python3
"""Create or list Romance Expert accounts (stored in data/users.db)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.settings import get_settings
from app.users_store import create_user, delete_user, init_db, list_users


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage Romance Expert user accounts")
    sub = parser.add_subparsers(dest="cmd", required=True)

    add_p = sub.add_parser("add", help="Create a user")
    add_p.add_argument("username")
    add_p.add_argument("password")

    sub.add_parser("list", help="List usernames")

    rm_p = sub.add_parser("remove", help="Delete a user")
    rm_p.add_argument("username")

    args = parser.parse_args()
    init_db(get_settings())

    if args.cmd == "add":
        user = create_user(args.username, args.password)
        print(f"Created user '{user.username}' (id={user.id})")
    elif args.cmd == "list":
        names = list_users()
        if not names:
            print("No users yet.")
        else:
            for name in names:
                print(name)
    elif args.cmd == "remove":
        if delete_user(args.username):
            print(f"Removed user '{args.username}'")
        else:
            print(f"User not found: {args.username}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
