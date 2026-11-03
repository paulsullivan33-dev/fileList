"""Move downloaded files or directories to configured destinations.

Reads the same downloaded.txt history file that superFetch writes. Each
line is ``title,category`` and the category selects the destination
through the ``destinations`` object in the JSON configuration file.

Use ``--dry-run`` to print what would move without moving anything.

Completed download folders are matched against superFetch's sanitized,
byte-limited NZB names, including the title/category identity hash.
Legacy folders named exactly after a release title are also supported.
Items keep their real names when moved.
"""

import argparse
import hashlib
import json
import os
import re
import download_moves
from utils import is_subpath
from datetime import datetime
from pathlib import Path


def download_folder_name(title, category):
    """Match superFetch/readAndParse.py:safe_nzb_filename without '.nzb'.

    Keep this naming contract in sync with the producer. The category is
    part of the identity, even when two records have the same release title.
    """
    safe_title = re.sub(r"[^\w.-]+", "-", title, flags=re.UNICODE).strip(" .-")
    safe_title = safe_title.encode("utf-8")[:150].decode("utf-8", errors="ignore") or "download"
    identity = hashlib.sha256((title + "\0" + category).encode("utf-8")).hexdigest()[:16]
    return f"{safe_title}-{identity}"


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
    history_value = config.get("downloaded_file", "downloaded.txt")
    if not isinstance(history_value, str) or not history_value.strip():
        raise ValueError("'downloaded_file' must be a non-empty path string.")
    downloaded_file = _resolve_path(
        history_value, base_directory
    )
    resolved_destinations = {}
    for key, value in destinations.items():
        if not isinstance(key, str) or not key.strip() or not isinstance(value, str) or not value.strip():
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
    generated_rules = {}
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
                # Preserve every title/category identity, including repeated
                # titles in different categories. Bare legacy names retain the
                # first category because they carry no identity information.
                generated_rules[download_folder_name(item_name, destination_key)] = destination_key
                rules.setdefault(item_name, destination_key)
    except OSError as exc:
        raise ValueError(f"Cannot read rules file {downloaded_file}: {exc}") from exc
    # Generated identities take precedence over a coincidentally identical
    # legacy title, regardless of history order.
    rules.update(generated_rules)
    return rules


def move_downloads(source_directory, rules, destinations, dry_run=False):
    if not source_directory.is_dir():
        raise ValueError(f"Source directory does not exist: {source_directory}")

    verb_move = "WOULD MOVE" if dry_run else "MOVED"
    verb_fail = "WOULD FAIL" if dry_run else "FAILED"

    source_directory = source_directory.resolve()
    items = [item for item in source_directory.iterdir() if item.name != download_moves.STATE_DIRECTORY]

    def process(state_root=None, blocked=None):
        moved, failed = 0, 0
        for source_item in items:
            destination_key = rules.get(source_item.name)
            if destination_key is None:
                continue
            destination_directory = destinations.get(destination_key)
            if destination_directory is None:
                print(f"{verb_fail} item={source_item} category={destination_key!r}: destination key not configured")
                failed += 1
                continue
            destination_item = destination_directory / source_item.name
            try:
                destination_directory = destination_directory.resolve()
                destination_item = destination_directory / source_item.name
                if destination_directory.exists() and not destination_directory.is_dir():
                    raise ValueError(f"Destination is not a directory: {destination_directory}")
                if not (source_item.is_symlink() or source_item.is_file() or source_item.is_dir()):
                    raise ValueError(f"Unsupported download item type: {source_item}")
                journal = download_moves.journal_path(source_directory / download_moves.STATE_DIRECTORY, source_item, destination_item)
                if not dry_run and journal.name in (blocked or {}):
                    raise ValueError(blocked[journal.name])
                if os.path.lexists(destination_item):
                    raise FileExistsError(f"Destination already exists: {destination_item}")
                if not source_item.is_symlink() and source_item.is_dir() and is_subpath(destination_item, source_item):
                    raise ValueError(f"Cannot move a folder into itself: {source_item}")
                if is_subpath(destination_item, source_directory / download_moves.STATE_DIRECTORY):
                    raise ValueError("Destination is reserved for download recovery data.")
                if dry_run:
                    print(f"{verb_move} {source_item} -> {destination_item}")
                else:
                    destination_directory.mkdir(parents=True, exist_ok=True)
                    download_moves.move(source_item, destination_item, state_root)
                    print(f"{verb_move} {source_item} -> {destination_item}")
                moved += 1
            except (OSError, RuntimeError, ValueError) as error:
                print(f"{verb_fail} {source_item} -> {destination_item}: {error}")
                failed += 1
        return moved, failed

    if dry_run or (not any(item.name in rules for item in items) and not (source_directory / download_moves.STATE_DIRECTORY).exists()):
        return process()
    with download_moves.source_lock(source_directory) as state_root:
        items = [item for item in source_directory.iterdir() if item.name != download_moves.STATE_DIRECTORY]
        blocked = download_moves.recover(state_root)
        for record, error in blocked.items():
            print(f"RECOVERY NEEDS REVIEW record={state_root / record}: {error}")
        return process(state_root, blocked)


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
        default=os.environ.get("FILELIST_MOVE_CONFIG", "move_downloads.json"),
        help="JSON configuration file (default: move_downloads.json)",
    )
    parser.add_argument(
        "--dry-run",
        "-n",
        action="store_true",
        help="print what would be moved without moving anything",
    )
    parser.add_argument("--check-mappings", action="store_true", help="Check observed superFetch categories without moving files")
    parser.add_argument("--superfetch-data", type=Path, help="Folder containing superFetch history and cached feed XML")
    args = parser.parse_args(argv)
    config_path = Path(args.config).expanduser().resolve()
    if args.check_mappings:
        import category_mappings
        report = category_mappings.check(config_path, args.superfetch_data)
        category_mappings.print_report(report)
        return 0 if report["status"] == "ok" else 1

    try:
        source_directory, downloaded_file, destinations = load_config(
            config_path
        )
        rules = load_rules(downloaded_file)
        moved, failed = move_downloads(
            source_directory, rules, destinations, dry_run=args.dry_run
        )
    except (OSError, RuntimeError, ValueError) as exc:
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
