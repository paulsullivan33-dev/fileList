#!/usr/bin/env python3
"""Add a user or update their password and groups without displaying passwords."""
import argparse
import getpass
import json
import os
from pathlib import Path
import tempfile
from werkzeug.security import generate_password_hash


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("username")
    parser.add_argument("--groups", required=True, help="Comma-separated directory/remote groups")
    args = parser.parse_args()
    password = getpass.getpass("Password: ")
    if not password or password != getpass.getpass("Confirm password: "):
        parser.error("Passwords must match and must not be empty")
    path = Path(__file__).resolve().with_name("users.json")
    users = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    if not isinstance(users, list):
        parser.error("users.json must contain an array")
    users = [user for user in users if user.get("username") != args.username]
    users.append(dict(username=args.username, password=generate_password_hash(password), groups=args.groups))
    handle, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as output:
            json.dump(users, output, indent=2)
            output.write("\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    print("User saved. Sign in again to use the new groups.")


if __name__ == "__main__":
    main()
