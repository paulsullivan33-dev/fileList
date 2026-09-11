"""Move downloaded files or directories to configured destinations.

Each non-empty row in downloaded.txt must contain:

    item_name,destination_key

The destination key is resolved through the ``destinations`` object in the
JSON configuration file.
"""

import argparse
import csv
import json
import shutil
from datetime import datetime
from pathlib import Path


def _resolve_path(value, base_directory):
    path = Path(value).expanduser()
    return path if path.is_absolute() else (base_directory / path).resolve()


def load_config(config_path):
    try:
        with config_path.open("r", encoding="utf-8") as config_file:
            config = json.load(config_file)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read configuration {config_path}: {exc}") from exc

    if not isinstance(config, dict):
        raise ValueError("Configuration must be a JSON object.")

    source_value = config.get("source_directory")
    destinations = config.get("destinations")
    if not isinstance(source_value, str) or not source_value.strip():
        raise ValueError("'source_directory' must be a non-empty string.")
    if not isinstance(destinations, dict):
        raise ValueError("'destinations' must be a JSON object.")

    base_directory = config_path.parent
    source_directory = _resolve_path(source_value, base_directory)
    downloaded_file = _resolve_path(
        config.get("downloaded_file", "downloaded.txt"), base_directory
    )
    resolved_destinations = {}
    for key, value in destinations.items():
        if not isinstance(key, str) or not isinstance(value, str) or not value.strip():
            raise ValueError("Every destination key and path must be a non-empty string.")
        resolved_destinations[key] = _resolve_path(value, base_directory)

    return source_directory, downloaded_file, resolved_destinations


def load_rules(downloaded_file):
    rules = {}
    try:
        with downloaded_file.open("r", encoding="utf-8-sig", newline="") as rule_file:
            for line_number, row in enumerate(csv.reader(rule_file), start=1):
                if not row or all(not field.strip() for field in row):
                    continue
                if len(row) < 2:
                    print(
                        f"Skipped rule line {line_number}: "
                        "expected item_name,destination_key"
                    )
                    continue
                item_name = row[0].strip()
                destination_key = row[1].strip()
                if not item_name or not destination_key:
                    print(f"Skipped rule line {line_number}: empty field")
                    continue
                if item_name in rules:
                    # The first occurrence is authoritative. Historical files can
                    # contain later duplicates, which should not change routing.
                    continue
                rules[item_name] = destination_key
    except OSError as exc:
        raise ValueError(f"Cannot read rules file {downloaded_file}: {exc}") from exc
    return rules


def move_downloads(source_directory, rules, destinations):
    if not source_directory.is_dir():
        raise ValueError(f"Source directory does not exist: {source_directory}")

    moved = 0
    failed = 0
    for source_item in source_directory.iterdir():
        destination_key = rules.get(source_item.name)
        if destination_key is None:
            continue

        destination_directory = destinations.get(destination_key)
        if destination_directory is None:
            print(
                f"CATEGORY NOT CONFIGURED item={source_item} "
                f"category={destination_key!r}"
            )
            failed += 1
            continue

        destination_item = destination_directory / source_item.name
        if destination_item.exists():
            print(
                f"FAILED {source_item} -> {destination_item}: "
                "destination already exists"
            )
            failed += 1
            continue

        try:
            destination_directory.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source_item), str(destination_item))
            print(f"MOVED {source_item} -> {destination_item}")
            moved += 1
        except OSError as exc:
            print(f"FAILED {source_item} -> {destination_item}: {exc}")
            failed += 1

    return moved, failed


def main(argv=None):
    print(f"START {datetime.now():%Y-%m-%d %H:%M:%S}")
    parser = argparse.ArgumentParser(
        description=(
            "Move files or directories according to downloaded.txt and a JSON "
            "configuration."
        )
    )
    parser.add_argument(
        "config",
        nargs="?",
        default="move_downloads.json",
        help="JSON configuration file (default: move_downloads.json)",
    )
    args = parser.parse_args(argv)
    config_path = Path(args.config).expanduser().resolve()

    try:
        source_directory, downloaded_file, destinations = load_config(
            config_path
        )
        rules = load_rules(downloaded_file)
        moved, failed = move_downloads(source_directory, rules, destinations)
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}")
        print(f"END {datetime.now():%Y-%m-%d %H:%M:%S}")
        return 1

    print(f"COMPLETE moved={moved} failed={failed}")
    print(f"END {datetime.now():%Y-%m-%d %H:%M:%S}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
