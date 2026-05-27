"""
processor.py
------------
Core logic that runs as a background task for each triggered row event.

Flow:
  1. Fetch the row + discussions from Smartsheet
  2. Apply skip rules (Complete, Claude last, [Skip] tag)
  3. Build a prompt with full card context
  4. Call Anthropic API with Smartsheet MCP + web_search attached
  5. Claude fetches fresh discussions, researches, and posts the comment itself
"""

import json
import logging
from datetime import datetime, timedelta, timezone

import httpx

from config import settings

log = logging.getLogger(__name__)

# ── Client name map — add your sheet IDs and client names here ────────────
# This is also driven by SHEET_IDS in .env; the map just provides friendly names.
# If a sheet ID isn't in this map the processor still works — it will use
# "Unknown Client" as the client name.
SHEET_CLIENT_MAP: dict[str, str] = {
    "5337282696400772": "Astellas",
    "5267249496543108": "Atria Senior Living",
    "901776017411972":  "Deloitte ITS",
    "8042391075245956": "Fitch Ratings",
    "6725738785886084": "HP Enterprise",
    "1818020853796740": "Johnson and Johnson",
    "5620664638590852": "Verizon",
    "2724886018477956": "Miscellaneous",
}

SMARTSHEET_BASE = "https://api.smartsheet.com/2.0"
ANTHROPIC_BASE  = "https://api.anthropic.com/v1"

AI_TAG   = "[Claude Research Note]"
SKIP_TAG = "[Skip]"


class RowNotFound(Exception):
    pass


# Rows currently being processed — prevents webhook + poll from doubling up
_in_flight: set[str] = set()


# ── Smartsheet helpers ────────────────────────────────────────────────────

def _ss_headers() -> dict:
    return {
        "Authorization": f"Bearer {settings.smartsheet_token}",
        "Content-Type": "application/json",
    }


def fetch_row(sheet_id: str, row_id: str) -> dict:
    import time
    url = f"{SMARTSHEET_BASE}/sheets/{sheet_id}/rows/{row_id}?include=discussions"
    for attempt in range(4):
        with httpx.Client(timeout=30) as client:
            r = client.get(url, headers=_ss_headers())
        if r.status_code == 404 and attempt < 3:
            wait = 2 ** attempt  # 1s, 2s, 4s
            log.info("Row not found yet, retrying in %ds (attempt %d/4)", wait, attempt + 1)
            time.sleep(wait)
            continue
        if r.status_code == 404:
            raise RowNotFound(f"row={row_id} not found after retries (likely blank row auto-deleted)")
        r.raise_for_status()
        return r.json()
    raise RowNotFound(f"row={row_id} not found after retries (likely blank row auto-deleted)")


# ── Skip-rule helpers ─────────────────────────────────────────────────────

def _flatten_comments(row: dict) -> list[dict]:
    comments: list[dict] = []
    for disc in row.get("discussions", []):
        comments.extend(disc.get("comments", []))
    comments.sort(key=lambda c: c.get("createdAt", ""), reverse=True)
    return comments


def should_skip(row: dict) -> tuple[bool, str]:
    """
    Returns (skip, reason).
    skip=True means we do NOT post a Claude note.
    """
    cells  = row.get("cells", [])
    status = cells[2].get("value", "") if len(cells) > 2 else ""

    if status == "Complete":
        return True, "Status is Complete"

    comments = _flatten_comments(row)
    if comments:
        most_recent_text = comments[0].get("text", "")
        if most_recent_text.startswith(AI_TAG):
            return True, "Claude was last commenter — no new human activity"
        if most_recent_text.startswith(SKIP_TAG):
            return True, "[Skip] tag on most recent comment"

    return False, ""


# ── Prompt builder ────────────────────────────────────────────────────────

def build_prompt(row: dict, sheet_id: str) -> str:
    client_name = SHEET_CLIENT_MAP.get(sheet_id, "Unknown Client")
    cells       = row.get("cells", [])

    def cell(i: int) -> str:
        return str(cells[i].get("value", "")) if len(cells) > i else ""

    task_name = cell(0) or "Untitled Task"
    owner     = cell(1)
    status    = cell(2)
    priority  = cell(3)
    due_date  = cell(4)
    notes     = cell(5)
    row_id    = str(row.get("id", ""))

    comments      = _flatten_comments(row)
    has_prior_note = any(c.get("text", "").startswith(AI_TAG) for c in comments)
    recent_text    = "\n".join(
        f"{c.get('createdBy', {}).get('name', 'Unknown')} ({c.get('createdAt', '')}): {c.get('text', '')}"
        for c in comments[:5]
    ) or "(none yet)"

    return f"""You are the Smartsheet Claude AI Processor for a Qlik consulting team.
Process ONLY this one card using your Smartsheet MCP tools.

== CARD CONTEXT ==
Sheet ID:   {sheet_id}
Row ID:     {row_id}
Client:     {client_name}
Task Name:  {task_name}
Owner:      {owner}
Status:     {status}
Priority:   {priority}
Due Date:   {due_date}
Notes:      {notes}

Recent comments (newest first):
{recent_text}

Has prior {AI_TAG}: {has_prior_note}

== YOUR TASK ==
1. Call Smartsheet:list_row_discussions (sheet_id: {sheet_id}, row_id: {row_id}, \
include_comments: true) to confirm the freshest comment state.

2. Apply skip rules:
   - STOP if Status = Complete
   - STOP if most recent comment starts with {AI_TAG}
   - STOP if most recent comment starts with {SKIP_TAG}
   If skipping, respond only: SKIPPED: <reason>

3. Use web_search: "{task_name} {client_name} Qlik best practices"
   Read the top results for relevant context.

4. If a prior {AI_TAG} exists, focus on what has CHANGED since then based on \
the latest human comments.

5. Post the following format as a comment (under 400 words, Markdown):

{AI_TAG}

\U0001f916 **AI Research & Next Steps**

*[1-2 sentence summary referencing {client_name} and technology context.]*

**Suggested next steps:**
- [Concrete, actionable step 1]
- [Concrete, actionable step 2]
- [Concrete, actionable step 3]
- [Optional step 4]
- [Optional step 5]

---
*Reply to this comment to trigger a follow-up research pass. \
Start your reply with [Skip] to leave a note without triggering Claude.*

6. Call Smartsheet:create_discussion_on_row:
   sheet_id: {sheet_id}
   row_id:   {row_id}
   comment body: your formatted note above

7. Reply with one line: \
"Posted {AI_TAG} to [{client_name}] '{task_name}'" or "SKIPPED: <reason>"
"""


# ── Anthropic API call ────────────────────────────────────────────────────

def call_claude(prompt: str) -> str:
    """
    Calls Anthropic API with:
      - Smartsheet MCP server  (Claude will use it to fetch discussions + post comment)
      - web_search tool        (Claude will use it for Qlik research)
    Returns Claude's final text summary.
    """
    payload = {
        "model": settings.claude_model,
        "max_tokens": 4096,
        "tools": [
            {"type": "web_search_20250305", "name": "web_search"}
        ],
        "mcp_servers": [
            {
                "type": "url",
                "url": "https://mcp.smartsheet.com",
                "name": "smartsheet-mcp",
                "authorization_token": settings.smartsheet_token,
            }
        ],
        "system": (
            "You are the Smartsheet Claude AI Processor for a Qlik consulting team. "
            "You have access to Smartsheet MCP tools and web search. "
            "Follow instructions precisely. Always use your tools — "
            "do not simulate or skip tool calls."
        ),
        "messages": [{"role": "user", "content": prompt}],
    }

    headers = {
        "x-api-key": settings.anthropic_api_key,
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "mcp-client-2025-04-04",
        "content-type": "application/json",
    }

    with httpx.Client(timeout=settings.anthropic_timeout) as client:
        r = client.post(
            f"{ANTHROPIC_BASE}/messages",
            headers=headers,
            json=payload,
        )
        if not r.is_success:
            log.error("Anthropic error %s: %s", r.status_code, r.text[:500])
        r.raise_for_status()
        data = r.json()

    content    = data.get("content", [])
    text_blocks = [b["text"] for b in content if b.get("type") == "text"]
    tools_used  = [b["name"] for b in content if b.get("type") == "tool_use"]

    summary = text_blocks[-1].strip() if text_blocks else "No summary returned"
    log.info("Claude finished  tools=%s  summary=%.120s", tools_used, summary)
    return summary


# ── Main entry point ──────────────────────────────────────────────────────

def _has_recent_activity(row: dict, since: datetime) -> bool:
    """True if a row has had human-driven changes since the given datetime."""
    modified_str = row.get("modifiedAt", "")
    if modified_str:
        try:
            modified = datetime.fromisoformat(modified_str.replace("Z", "+00:00"))
            if modified >= since:
                return True
        except ValueError:
            pass

    for disc in row.get("discussions", []):
        for comment in disc.get("comments", []):
            if comment.get("text", "").startswith(AI_TAG):
                continue
            created_str = comment.get("createdAt", "")
            if created_str:
                try:
                    created = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
                    if created >= since:
                        return True
                except ValueError:
                    pass
    return False


def find_unprocessed_rows(sheet_id: str, since_minutes: int = 0) -> list[dict]:
    """
    Fetch the sheet and return rows that need a Claude comment.
    If since_minutes > 0, only returns rows with human activity in that window
    (used by the polling loop to avoid reprocessing old untouched rows).
    """
    url = f"{SMARTSHEET_BASE}/sheets/{sheet_id}?include=discussions"
    with httpx.Client(timeout=30) as client:
        r = client.get(url, headers=_ss_headers())
        r.raise_for_status()
    rows = r.json().get("rows", [])

    since = (
        datetime.now(timezone.utc) - timedelta(minutes=since_minutes)
        if since_minutes > 0 else None
    )

    result = []
    for row in rows:
        if since and not _has_recent_activity(row, since):
            continue
        skip, _ = should_skip(row)
        if not skip:
            result.append(row)
    return result


def process_row_event(sheet_id: str, row_id: str) -> None:
    """
    Called as a FastAPI background task for each triggered row event.
    All exceptions are caught and logged — we never want a background
    task crash to bubble up to the webhook response.
    """
    key = f"{sheet_id}:{row_id}"
    if key in _in_flight:
        log.info("Skipping duplicate in-flight event  sheet=%s  row=%s", sheet_id, row_id)
        return
    _in_flight.add(key)

    try:
        log.info("Processing  sheet=%s  row=%s", sheet_id, row_id)

        try:
            row = fetch_row(sheet_id, row_id)
        except RowNotFound:
            log.warning("Row not available after retries — will be caught by next poll  sheet=%s  row=%s", sheet_id, row_id)
            return
        except Exception as exc:
            log.error("Failed to fetch row  sheet=%s  row=%s  err=%s", sheet_id, row_id, exc)
            return

        skip, reason = should_skip(row)
        if skip:
            log.info("Skipped  sheet=%s  row=%s  reason=%s", sheet_id, row_id, reason)
            return

        prompt = build_prompt(row, sheet_id)

        try:
            summary = call_claude(prompt)
            log.info("Done  sheet=%s  row=%s  result=%.80s", sheet_id, row_id, summary)
        except Exception as exc:
            log.error("Claude call failed  sheet=%s  row=%s  err=%s", sheet_id, row_id, exc)

    finally:
        _in_flight.discard(key)
