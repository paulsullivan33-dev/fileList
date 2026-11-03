"""Folder-wide search/sort with metadata reads limited to the displayed page."""
import os
import stat


PAGE_SIZES = (25, 50, 100, 250)


def _integer(value, default):
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def _details(record):
    if "stat" not in record:
        try:
            record["stat"] = record["entry"].stat()
        except OSError:
            record["stat"] = None
    return record


def page(directory, page_number=1, page_size=100, query="", sort=1, order="asc"):
    page_size = _integer(page_size, 100)
    if page_size not in PAGE_SIZES:
        page_size = 100
    sort = _integer(sort, 1)
    if sort not in (1, 3, 4):
        sort = 1
    order = "desc" if order == "desc" else "asc"
    query = query.strip()
    needle = query.casefold()
    records = []
    with os.scandir(directory) as entries:
        for entry in entries:
            if needle not in entry.name.casefold():
                continue
            try:
                is_directory = entry.is_dir()
            except OSError:
                is_directory = False
            records.append({"name": entry.name, "is_dir": is_directory, "entry": entry})
    records.sort(key=lambda record: (record["name"].casefold(), record["name"]))
    if sort in (3, 4):
        # Global size/date ordering needs these values for all matches, but
        # normal name browsing does not pay that cost.
        for record in records:
            _details(record)
        records.sort(key=lambda record: (getattr(record["stat"], "st_size" if sort == 3 else "st_mtime", -1)),
                     reverse=order == "desc")
    elif order == "desc":
        records.reverse()
    records.sort(key=lambda record: not record["is_dir"])
    total = len(records)
    pages = max(1, (total + page_size - 1) // page_size)
    number = max(1, min(_integer(page_number, 1), pages))
    start = (number - 1) * page_size
    selected = [_details(record) for record in records[start:start + page_size]]
    return {"entries": selected, "page": number, "page_size": page_size, "pages": pages,
            "total": total, "start": start + 1 if total else 0, "end": start + len(selected),
            "query": query, "sort": sort, "order": order}


def totals(directory):
    result = {"files": 0, "folders": 0, "bytes": 0, "unavailable": 0}
    with os.scandir(directory) as entries:
        for entry in entries:
            try:
                info = entry.stat()
            except OSError:
                result["unavailable"] += 1
                continue
            if stat.S_ISDIR(info.st_mode):
                result["folders"] += 1
            else:
                result["files"] += 1
                result["bytes"] += info.st_size
    return result
