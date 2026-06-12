"""
Smartsheet Claude AI Processor
--------------------------------
FastAPI webhook receiver that:
  1. Handles Smartsheet verification challenges instantly
  2. Receives row create/update events
  3. Fires Claude (with Smartsheet MCP + web search) as a background task
  4. Claude posts a [Claude Research Note] comment back to the row
"""

import asyncio
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import BackgroundTasks, FastAPI, Request, Response

from config import settings
from processor import find_unprocessed_rows, process_discussion_event, process_row_event

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)
log = logging.getLogger(__name__)


async def _polling_loop():
    interval = settings.poll_interval_mins * 60
    log.info("Polling loop starting — interval=%dm", settings.poll_interval_mins)
    await asyncio.sleep(interval)  # don't hammer on startup, wait one full interval first
    while True:
        try:
            log.info("Polling sheets for unprocessed rows")
            total = 0
            for sheet_id in settings.sheet_ids:
                try:
                    rows = await asyncio.to_thread(
                        find_unprocessed_rows, sheet_id, settings.poll_interval_mins * 2
                    )
                    for row in rows:
                        row_id = str(row["id"])
                        log.info("Poll queuing  sheet=%s  row=%s", sheet_id, row_id)
                        asyncio.create_task(asyncio.to_thread(process_row_event, sheet_id, row_id))
                        total += 1
                except Exception as exc:
                    log.error("Poll error for sheet=%s  err=%s", sheet_id, exc)
            log.info("Poll complete — %d row(s) queued", total)
        except Exception as exc:
            log.error("Polling loop error: %s", exc)
        await asyncio.sleep(interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Smartsheet Claude Processor starting up")
    log.info("Watching %d sheet(s)", len(settings.sheet_ids))
    poll_task = asyncio.create_task(_polling_loop())
    yield
    poll_task.cancel()
    log.info("Shutting down")


app = FastAPI(title="Smartsheet Claude Processor", lifespan=lifespan)


@app.post("/webhook")
async def webhook(request: Request, background_tasks: BackgroundTasks):
    """
    Single endpoint for all Smartsheet webhook callbacks.
    Handles:
      - Verification challenge  (must respond synchronously)
      - Row / comment events    (processed in background)
    """
    body = await request.json()

    # ── Verification challenge ────────────────────────────────────────────
    # Smartsheet sends {"webhookId": ..., "challenge": "..."}
    # We must echo the challenge value back immediately.
    challenge = body.get("challenge")
    if challenge:
        log.info("Verification challenge received — echoing back")
        return {"smartsheetHookResponse": challenge}

    # ── Event callback ────────────────────────────────────────────────────
    scope_object_id = str(body.get("scopeObjectId", ""))
    events = body.get("events", [])

    if not events:
        return Response(status_code=200)

    # Validate sheet is in our target list
    if scope_object_id not in settings.sheet_ids:
        log.info("Ignoring event for untracked sheet %s", scope_object_id)
        return Response(status_code=200)

    # Deduplicate — one task per unique row/discussion per callback
    seen_rows: set[str] = set()
    seen_discussions: set[str] = set()
    for event in events:
        action      = event.get("eventType", "")
        object_type = event.get("objectType", "")
        event_id    = str(event.get("id", ""))

        if action not in ("created", "updated"):
            continue

        if object_type == "discussion":
            # Comment added to a row — event.id is the discussion ID.
            # We must fetch the discussion to resolve the row ID; done inside
            # process_discussion_event so the webhook response stays instant.
            if not event_id or event_id in seen_discussions:
                continue
            seen_discussions.add(event_id)
            log.info(
                "Queuing discussion resolver  sheet=%s  discussion=%s",
                scope_object_id, event_id,
            )
            background_tasks.add_task(
                process_discussion_event,
                sheet_id=scope_object_id,
                discussion_id=event_id,
            )

        elif object_type in ("row", "cell"):
            # Row created/updated — event has rowId (cell) or id (row)
            row_id = str(event.get("rowId") or event.get("id") or "")
            if not row_id or row_id in seen_rows:
                continue
            seen_rows.add(row_id)
            log.info(
                "Queuing processor  sheet=%s  row=%s  objectType=%s",
                scope_object_id, row_id, object_type,
            )
            background_tasks.add_task(
                process_row_event,
                sheet_id=scope_object_id,
                row_id=row_id,
                object_type=object_type,
            )
        # comment and sheet objectTypes are ignored — discussion events cover them

    # Return 200 immediately — Claude runs in the background
    return Response(status_code=200)


@app.get("/health")
async def health():
    """Simple liveness check."""
    return {"status": "ok", "sheets_watched": len(settings.sheet_ids)}
