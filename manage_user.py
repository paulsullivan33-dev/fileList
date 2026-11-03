#!/usr/bin/env python3
"""Add a user or update their password and groups without displaying passwords."""
import argparse
import getpass
import json
from pathlib import Path
import config_backups
from werkzeug.security import generate_password_hash


USERS_FILE = Path(__file__).resolve().with_name("users.json")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("username")
    parser.add_argument("--groups", required=True, help="Comma-separated directory/remote groups")
    args = parser.parse_args(argv)
    password = getpass.getpass("Password: ")
    if not password or password != getpass.getpass("Confirm password: "):
        parser.error("Passwords must match and must not be empty")
    path = USERS_FILE
    users = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    if not isinstance(users, list):
        parser.error("users.json must contain an array")
    users = [user for user in users if user.get("username") != args.username]
    users.append(dict(username=args.username, password=generate_password_hash(password), groups=args.groups))
    try:
        previous = config_backups.write_json(path, users)
    except OSError as error:
        parser.error(f"User settings were not saved: {error}")
    if previous:
        print("Previous user settings:", previous)
    print("User saved. Sign in again to use the new groups.")


if __name__ == "__main__":
    main()
