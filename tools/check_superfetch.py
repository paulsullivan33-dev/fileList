"""Compare fileList's download contract with the actual superFetch producer."""
import argparse
import ast
import hashlib
import os
from pathlib import Path
import re
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from move_downloads import download_folder_name, load_rules


def producer_path():
    return Path(os.environ.get("SUPERFETCH_SOURCE", ROOT.parent / "superFetch" / "readAndParse.py")).expanduser()


def load_contract(path):
    """Load just the two pure contract functions, without running downloads."""
    path = Path(path)
    if path.is_dir():
        path = path / "readAndParse.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names = {"safe_nzb_filename", "load_downloaded"}
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    if {node.name for node in functions} != names:
        raise ValueError("superFetch's filename/history functions changed or are missing.")
    namespace = {"hashlib": hashlib, "re": re, "Path": Path}
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(path), "exec"), namespace)
    return namespace


def check(path):
    producer = load_contract(path)
    titles = ["Example.Release", "Example Release", "Example, Release", "A/B:C?D",
              "\u6f22" * 60, "\U0001f642" * 60, "a" * 151, "???", ".hidden..release", "release-0123456789abcdef"]
    categories = ["Books", "Comics", "Audio Books"]
    errors = []
    for title in titles:
        for category in categories:
            expected = producer["safe_nzb_filename"](title, category)
            actual = download_folder_name(title, category) + ".nzb"
            if actual != expected:
                errors.append(f"Filename contract mismatch for {title!r}, {category!r}: fileList={actual!r}; superFetch={expected!r}")
    with tempfile.TemporaryDirectory(prefix="filelist-contract-") as temporary:
        history = Path(temporary) / "downloaded.txt"
        records = [(title, category) for title in titles for category in categories]
        history.write_text("\n".join(f"{title},{category}" for title, category in records) + "\n", encoding="utf-8")
        parsed = producer["load_downloaded"](history)
        if parsed != set(records):
            errors.append("superFetch's downloaded.txt history format no longer matches title,category records.")
        rules = load_rules(history)
        for title, category in records:
            filename = producer["safe_nzb_filename"](title, category)
            if not filename.endswith(".nzb") or rules.get(filename[:-4]) != category:
                errors.append(f"Routing contract mismatch for {title!r}, {category!r}")
    return errors


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--superfetch", type=Path, default=producer_path(), help="superFetch folder or readAndParse.py")
    args = parser.parse_args(argv)
    try:
        errors = check(args.superfetch)
    except Exception as error:
        print(f"superFetch compatibility check failed: {error}")
        return 1
    if errors:
        for error in errors:
            print(error)
        return 1
    print("superFetch compatibility passed: 30 filename cases and 30 history/routing cases.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
