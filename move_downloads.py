"""Move downloaded files or directories to configured destinations.

Reads the same downloaded.txt history file that superFetch writes. Each
line is ``title,category`` and the category selects the destination
through the ``destinations`` object in the JSON configuration file.

Use ``--dry-run`` to print what would move without moving anything.

Completed download folders are matched by release title: SABnzbd names
them after the NZB filename, which superFetch suffixes with a 16-hex
identity hash, so that suffix is stripped for matching (the item keeps
its real name when moved).
"""

import argparse
import json
import re
import shutil
from datetime import datetime
from pathlib import Path


# SABnzbd names each completed job folder after the NZB filename, and
# superFetch saves NZBs as {title}-{16 hex chars} (see safe_nzb_filename in
# readAndParse.py). Strip that suffix when matching rules; the item keeps
# its real name when moved.
NZB_NAME_SUFFIX = re.compile(r"-[0-9a-f]{16}$")


def match_name(name):
    """The rule key for a source item: exact name, else the name without
    superFetch's NZB identity suffix."""
    return NZB_NAME_SUFFIX.sub("", name)


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
    """Reads the shared downloaded.txt history (the same file superFetch writes).

    Each line is ``title,category``. The split is on the LAST comma, exactly
    like superFetch parses it, so titles containing commas still map to the
    right item and destination.
    """
    rules = {}
    try:
        with downloaded_file.open("r", encoding="utf-8-sig") as rule_file:
            for line_number, line in enumerate(rule_file, start=1):
                record = line.rstrip("\r\n")
                if not record.strip():
                    continue
                item_name, separator, destination_key = record.rpartition(",")
                item_name = item_name.strip()
                destination_key = destination_key.strip()
                if not separator:
                    print(
                        f"Skipped rule line {line_number}: "
                        "expected title,category"
                    )
                    continue
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


def move_downloads(source_directory, rules, destinations, dry_run=False):
    if not source_directory.is_dir():
        raise ValueError(f"Source directory does not exist: {source_directory}")

    verb_move = "WOULD MOVE" if dry_run else "MOVED"
    verb_fail = "WOULD FAIL" if dry_run else "FAILED"

    moved = 0
    failed = 0
    for source_item in source_directory.iterdir():
        destination_key = rules.get(source_item.name)
        if destination_key is None:
            # Completed folders carry superFetch's NZB identity suffix;
            # the rules file has the bare release title.
            destination_key = rules.get(match_name(source_item.name))
        if destination_key is None:
            continue

        destination_directory = destinations.get(destination_key)
        if destination_directory is None:
            if dry_run:
                print(
                    f"WOULD FAIL item={source_item} "
                    f"category={destination_key!r}: "
                    "destination key not configured"
                )
            else:
                print(
                    f"CATEGORY NOT CONFIGURED item={source_item} "
                    f"category={destination_key!r}"
                )
            failed += 1
            continue

        destination_item = destination_directory / source_item.name
        if destination_item.exists():
            print(
                f"{verb_fail} {source_item} -> {destination_item}: "
                "destination already exists"
            )
            failed += 1
            continue

        try:
            if dry_run:
                print(f"{verb_move} {source_item} -> {destination_item}")
            else:
                destination_directory.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source_item), str(destination_item))
                print(f"{verb_move} {source_item} -> {destination_item}")
            moved += 1
        except OSError as exc:
            print(f"{verb_fail} {source_item} -> {destination_item}: {exc}")
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
    parser.add_argument(
        "--dry-run",
        "-n",
        action="store_true",
        help="print what would be moved without moving anything",
    )
    args = parser.parse_args(argv)
    config_path = Path(args.config).expanduser().resolve()

    try:
        source_directory, downloaded_file, destinations = load_config(
            config_path
        )
        rules = load_rules(downloaded_file)
        moved, failed = move_downloads(
            source_directory, rules, destinations, dry_run=args.dry_run
        )
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}")
        print(f"END {datetime.now():%Y-%m-%d %H:%M:%S}")
        return 1

    summary = f"COMPLETE moved={moved} failed={failed}"
    if args.dry_run:
        summary += " (dry run)"
    print(summary)
    print(f"END {datetime.now():%Y-%m-%d %H:%M:%S}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
