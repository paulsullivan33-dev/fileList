"""Create, list, or restore dated fileList JSON configuration backups."""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import config_backups


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("backup", help="Back up a JSON configuration file")
    create.add_argument("path", type=Path)
    listing = commands.add_parser("list", help="List backups for a configuration file")
    listing.add_argument("path", type=Path)
    restore = commands.add_parser("restore", help="Restore a backup directory, backing up current settings first")
    restore.add_argument("backup", type=Path)
    restore.add_argument("--target", type=Path, help="Restore to a different configuration path")
    args = parser.parse_args(argv)
    try:
        if args.command == "backup":
            print("Configuration backup:", config_backups.backup(args.path))
        elif args.command == "list":
            backups = config_backups.list_backups(args.path)
            for entry in backups:
                print(entry["created_at"], entry["backup"])
            if not backups:
                print("No configuration backups found.")
        else:
            target, previous = config_backups.restore(args.backup, args.target)
            print("Restored:", target)
            if previous:
                print("Backup of settings before restore:", previous)
    except (OSError, ValueError, KeyError, TypeError) as error:
        print("Configuration backup failed:", error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
