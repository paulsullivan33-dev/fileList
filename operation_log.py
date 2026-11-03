"""Durable, searchable operation records shared by Flask and the worker."""
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path


def database_path():
    import job_store
    return Path(os.environ.get("FILELIST_OPERATION_LOG", job_store.job_root() / "operations.sqlite3")).expanduser()


def _connect():
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path), timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("""CREATE TABLE IF NOT EXISTS operations (
        id INTEGER PRIMARY KEY, timestamp TEXT NOT NULL, username TEXT NOT NULL,
        operation TEXT NOT NULL, outcome TEXT NOT NULL, source TEXT NOT NULL,
        destination TEXT NOT NULL, item TEXT NOT NULL, job_id TEXT NOT NULL,
        detail TEXT NOT NULL)""")
    connection.execute("CREATE INDEX IF NOT EXISTS operations_owner_id ON operations(username, id)")
    return connection


def record(username, operation, outcome, source="", destination="", item="", job_id="", detail=""):
    """Logging trouble must never turn a successful file operation into a retry."""
    try:
        connection = _connect()
        try:
            with connection:
                connection.execute(
                    "INSERT INTO operations(timestamp,username,operation,outcome,source,destination,item,job_id,detail) VALUES(?,?,?,?,?,?,?,?,?)",
                    (datetime.now(timezone.utc).isoformat(), str(username or ""), str(operation), str(outcome),
                     str(source), str(destination), str(item), str(job_id), str(detail or "")),
                )
        finally:
            connection.close()
        return True
    except (OSError, sqlite3.Error) as error:
        logging.getLogger(__name__).warning("Could not write operation log: %s", error)
        return False


def _location(selection, item=""):
    remote = selection.get("configuration")
    if isinstance(remote, dict):
        base = f"{remote.get('host', '')}:{remote.get('path', '')}".rstrip("/")
        return base + ("/" + item if item else "")
    base = selection.get("directory_root") or f"directory #{selection.get('directory_index', '?')}"
    return os.path.join(str(base), str(selection.get("path") or ""), str(item or ""))


def record_job(job, outcome, item="", detail=""):
    item = item or ""
    names = job.get("source", {}).get("names", [])
    target_item = os.path.splitext(item)[0] + ".mp4" if item and job.get("operation") == "convert" else item
    return record(job.get("owner"), job.get("operation", "job"), outcome,
                  _location(job.get("source", {}), item), _location(job.get("destination", {}), target_item),
                  item=item or ", ".join(str(name) for name in names), job_id=job.get("job_id", ""), detail=detail)


def search(username, query="", operation="", outcome="", limit=50, offset=0):
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    terms, parameters = ["username = ?"], [username]
    if query:
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        terms.append("(source || ' ' || destination || ' ' || item || ' ' || detail || ' ' || job_id || ' ' || operation) LIKE ? ESCAPE '\\'")
        parameters.append("%" + escaped + "%")
    for field, value in (("operation", operation), ("outcome", outcome)):
        if value:
            terms.append(field + " = ?")
            parameters.append(value)
    connection = _connect()
    try:
        rows = connection.execute("SELECT * FROM operations WHERE " + " AND ".join(terms)
                                  + " ORDER BY id DESC LIMIT ? OFFSET ?", parameters + [limit, offset]).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def prune(days=30):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    connection = _connect()
    try:
        with connection:
            connection.execute("DELETE FROM operations WHERE timestamp < ?", (cutoff,))
    finally:
        connection.close()
