"""Check observed superFetch category labels against download destinations."""
import ast
import json
import os
from pathlib import Path
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parent


def config_path():
    return Path(os.environ.get("FILELIST_MOVE_CONFIG", ROOT / "move_downloads.json")).expanduser()


def data_directory():
    source = Path(os.environ.get("SUPERFETCH_SOURCE", ROOT.parent / "superFetch" / "readAndParse.py")).expanduser()
    source_root = source if source.is_dir() else source.parent
    return Path(os.environ.get("SUPERFETCH_DATA_DIR", source_root)).expanduser()


def _feed_names(source_root):
    """Read the feed catalog as data; importing superFetch could run setup code."""
    tree = ast.parse((source_root / "feeds.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "FEEDS" for target in node.targets):
            if isinstance(node.value, (ast.Tuple, ast.List)):
                return [entry.args[1].value for entry in node.value.elts
                        if isinstance(entry, ast.Call) and len(entry.args) > 1
                        and isinstance(entry.args[1], ast.Constant) and isinstance(entry.args[1].value, str)]
    return []


def check(path=None, data=None):
    from move_downloads import load_config
    path = Path(path or config_path()).expanduser().resolve()
    report = {"status": "not_configured", "categories": [], "missing": [], "warnings": [],
              "history_files": 0, "feed_files": 0, "missing_feed_files": 0}
    if not path.exists():
        report["warnings"].append("Download routing is not configured. Create move_downloads.json or set FILELIST_MOVE_CONFIG.")
        return report
    try:
        _, history, destinations = load_config(path)
    except (OSError, ValueError, TypeError) as error:
        report.update(status="invalid", warnings=[str(error)])
        return report
    data = Path(data or data_directory()).expanduser().resolve()
    observed = {}
    def observe(category, kind):
        if category:
            counts = observed.setdefault(category, {"history_count": 0, "feed_count": 0})
            counts[kind] += 1
    histories = {history.resolve()}
    producer_history = data / "downloaded.txt"
    if producer_history.exists():
        histories.add(producer_history.resolve())
    for candidate in sorted(histories):
        try:
            with candidate.open(encoding="utf-8-sig") as source:
                for line in source:
                    title, separator, category = line.rstrip("\r\n").rpartition(",")
                    if separator and title.strip() and category.strip():
                        observe(category.strip(), "history_count")
            report["history_files"] += 1
        except (OSError, ValueError):
            report["warnings"].append("A download history could not be read; category coverage is incomplete.")
    if history.resolve() != producer_history.resolve() and producer_history.exists():
        report["warnings"].append("The configured history differs from the discovered superFetch history. Confirm this is an intentional copy or point both tools at the shared history.")
    source = Path(os.environ.get("SUPERFETCH_SOURCE", ROOT.parent / "superFetch" / "readAndParse.py")).expanduser()
    source_root = source if source.is_dir() else source.parent
    try:
        names = _feed_names(source_root)
    except (OSError, ValueError, SyntaxError):
        names = []
    if not names:
        names = sorted(file.name for file in data.glob("*.xml"))
        report["warnings"].append("The superFetch feed catalog is unavailable; only available history and XML caches were checked.")
    for name in names:
        feed = data / name
        if not feed.exists():
            report["missing_feed_files"] += 1
            continue
        try:
            with feed.open("rb") as source_file:
                for _, element in ET.iterparse(source_file, events=("end",)):
                    if element.tag == "item":
                        observe(" ".join((element.findtext("category") or "").split()), "feed_count")
                        element.clear()
            report["feed_files"] += 1
        except (OSError, ET.ParseError):
            report["warnings"].append(f"Cached feed {name} could not be read; category coverage is incomplete.")
    if report["missing_feed_files"]:
        report["warnings"].append(f"{report['missing_feed_files']} configured feed cache(s) are missing; their category labels cannot be checked yet.")
    for category in sorted(observed, key=str.casefold):
        mapped = category in destinations
        report["categories"].append({"category": category, "mapped": mapped, **observed[category]})
        if not mapped:
            report["missing"].append(category)
    if not observed:
        report["warnings"].append("No category labels were found in the available history or feed caches.")
    report["status"] = ("missing" if report["missing"] else "incomplete" if report["warnings"] or not observed else "ok")
    return report


def print_report(report):
    print(f"Category mapping status: {report['status']}")
    for item in report["categories"]:
        print(f"{'MAPPED' if item['mapped'] else 'MISSING DESTINATION'}: {item['category']}")
    for warning in report["warnings"]:
        print("NOTE:", warning)


def public_report(report):
    # Category labels/counts are useful here; configuration paths and downloaded
    # titles are deliberately not included in the web response.
    result = dict(report)
    if result["status"] == "invalid":
        result["warnings"] = ["Download routing configuration could not be read. Check it with the command-line checker for details."]
    return result


def web_controls():
    return """<h2>Download category mappings</h2>
<button type='button' id='checkCategoryMappings'>Check category mappings</button>
<div id='categoryMappings' aria-live='polite'></div>
<script>
document.getElementById('checkCategoryMappings').onclick = async function () {
    const output = document.getElementById('categoryMappings');
    this.disabled = true; output.textContent = 'Checking observed categories…';
    try {
        const response = await fetch('/category_mappings');
        const data = await response.json().catch(() => ({}));
        if (!response.ok || !data.success) throw new Error(data.error || 'Could not check category mappings');
        output.replaceChildren();
        const summary = document.createElement('p');
        summary.textContent = `Status: ${data.status}. ${data.categories.length} observed categories; ${data.missing.length} missing destination(s).`;
        output.appendChild(summary);
        const list = document.createElement('ul');
        for (const item of data.categories) {
            const row = document.createElement('li');
            row.textContent = item.category + (item.mapped ? ' — Mapped' : ' — MISSING DESTINATION');
            list.appendChild(row);
        }
        output.appendChild(list);
        for (const message of data.warnings) { const note = document.createElement('p'); note.textContent = message; output.appendChild(note); }
    } catch (error) { output.textContent = error.message; }
    finally { this.disabled = false; }
};
</script>"""
