import json
import os
from datetime import datetime

from fastapi import APIRouter, Query

from proxy.logger import LOG_FILE

router = APIRouter()


@router.get("/logs")
async def get_logs(
    backend: str | None = Query(None),
    model: str | None = Query(None),
    limit: int = Query(50, ge=1, le=1000),
    since: str | None = Query(None, description="ISO date, e.g. 2026-04-25"),
):
    # Try PostgreSQL read replica first (fast indexed queries)
    from proxy.logger import query_logs_pg
    pg_results = query_logs_pg(backend=backend, model=model, since=since, limit=limit)
    if pg_results is not None:
        return {"entries": pg_results, "total": len(pg_results), "source": "postgresql"}

    # Fall back to JSONL file scan
    if not os.path.exists(LOG_FILE):
        return {"entries": [], "total": 0}

    since_dt = None
    if since:
        try:
            since_dt = datetime.fromisoformat(since)
        except ValueError:
            return {"error": f"Invalid date format: {since}. Use ISO format."}

    entries = []
    with open(LOG_FILE) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            if backend and entry.get("backend") != backend:
                continue
            if model and entry.get("model") != model:
                continue
            if since_dt:
                ts = entry.get("timestamp", "")
                try:
                    entry_dt = datetime.fromisoformat(ts)
                    # Strip timezone info for comparison if needed
                    if entry_dt.tzinfo and not since_dt.tzinfo:
                        entry_dt = entry_dt.replace(tzinfo=None)
                    elif since_dt.tzinfo and not entry_dt.tzinfo:
                        since_dt = since_dt.replace(tzinfo=None)
                    if entry_dt < since_dt:
                        continue
                except ValueError:
                    continue

            entries.append(entry)

    entries = entries[-limit:]
    return {"entries": entries, "total": len(entries)}


@router.get("/spend")
async def get_spend():
    from proxy.spend import get_spend_summary
    return get_spend_summary()
