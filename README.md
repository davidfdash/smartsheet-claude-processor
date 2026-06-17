# Smartsheet Claude AI Processor

Automatically posts AI-researched next steps as comments on Smartsheet rows whenever a card is created, updated, or commented on. Powered by Claude with Smartsheet MCP and web search.

Each user self-hosts their own instance. No shared infrastructure — your Smartsheet token and Anthropic key stay on your own machine or server.

---

## How it works

```
Smartsheet row created / updated / commented on
               │
               ▼
    POST /webhook  (FastAPI)
               │
               ├─ Returns 200 immediately  ← Smartsheet never times out
               │
               └─ Background task
                       │
                       ├─ Row / cell event  → fetch row by row ID
                       │
                       └─ Discussion event  → fetch discussion to resolve row ID
                               │
                          Anthropic API
                          Claude + Smartsheet MCP + web_search
                               │
                          Claude re-fetches live discussions,
                          searches for Qlik best practices,
                          posts [Claude Research Note] comment
```

A polling loop (default every 10 min) also scans sheets for rows with recent activity that the webhook may have missed.

### Smartsheet webhook event types

When a comment is added to a row, Smartsheet fires `objectType:discussion` events (not `objectType:comment`). The discussion event only contains the discussion ID — not the row ID. The processor resolves this by calling `GET /sheets/{sheetId}/discussions/{discussionId}` to get the `parentId` (the row ID), then processes that row normally with a 5-second propagation delay.

| Smartsheet objectType | Handled | How |
|---|---|---|
| `row` | ✅ | Row ID comes directly from `event.id` |
| `cell` | ✅ | Row ID comes from `event.rowId` |
| `discussion` | ✅ | Fetches discussion to resolve row ID |
| `comment` | ignored | Discussion events cover these |
| `sheet` | ignored | No row to process |

---

## Requirements

- Python 3.11+
- A **publicly reachable HTTPS URL** for your machine (ngrok for local dev, Railway/Render/VPS for production)
- [Smartsheet API token](https://smartsheet.com) — Account → Personal Settings → API Access
- [Anthropic API key](https://console.anthropic.com) — must have access to `claude-sonnet-4-6` and the `mcp-client` beta

---

## Quick start

### 1. Clone the repo

```bash
git clone https://github.com/davidfdash/smartsheet-claude-processor.git
cd smartsheet-claude-processor
```

### 2. Install dependencies

```bash
python -m venv venv
# Windows:
venv\Scripts\activate
# macOS/Linux:
source venv/bin/activate

pip install -r requirements.txt
```

### 3. Configure your environment

```bash
cp .env.example .env
```

Edit `.env` and fill in all required values:

| Variable | Required | Description |
|---|---|---|
| `SMARTSHEET_TOKEN` | ✅ | Smartsheet API access token |
| `ANTHROPIC_API_KEY` | ✅ | Anthropic API key |
| `SHEET_IDS` | ✅ | Comma-separated sheet IDs to watch |
| `CLAUDE_MODEL` | | Model to use (default: `claude-sonnet-4-6`) |
| `ANTHROPIC_TIMEOUT` | | Seconds to wait for Claude (default: `180`) |
| `POLL_INTERVAL_MINS` | | How often to poll for missed rows (default: `10`) |
| `PORT` | | Server port (default: `8000`) |

**Finding sheet IDs:** Open any Smartsheet sheet, go to File → Properties, or read the ID from the URL.

> **Windows `.env` note:** Use only `KEY=VALUE` format — no spaces around `=`, no quotes unless the value contains spaces. Lines in any other format are silently ignored by python-dotenv.

### 4. Map sheet IDs to client names

Open `processor.py` and update `SHEET_CLIENT_MAP` with your sheet IDs:

```python
SHEET_CLIENT_MAP: dict[str, str] = {
    "5337282696400772": "Acme Corp",
    "5267249496543108": "Globex",
    # add all sheets in SHEET_IDS here
}
```

The client name appears in Claude's comment and in the web search query. Unmapped sheets still work — Claude uses `"Unknown Client"`.

### 5. Start the app

```bash
python -m uvicorn main:app --host 0.0.0.0 --port 8000
```

You should see:
```
INFO  Smartsheet Claude Processor starting up
INFO  Watching 3 sheet(s)
INFO  Polling loop starting — interval=10m
```

### 6. Expose the app publicly

Smartsheet must be able to POST to your app over HTTPS.

**Local dev — ngrok:**
```bash
# First-time setup (one-time):
ngrok config add-authtoken YOUR_NGROK_TOKEN

# Start tunnel (use --domain for a persistent URL on paid/free static plans):
ngrok http --domain=YOUR-SUBDOMAIN.ngrok-free.app 8000

# Or without a static domain (URL changes each restart):
ngrok http 8000
```

If you have a static ngrok domain, use it consistently so you don't need to re-register webhooks after each restart.

**Production — Railway (recommended):**
1. Push this repo to GitHub
2. New project on [railway.app](https://railway.app) → Deploy from GitHub repo
3. Add environment variables in the Railway dashboard
4. Railway provides a public HTTPS URL automatically

**Production — Render:**
1. New Web Service on [render.com](https://render.com) → connect your GitHub repo
2. Set build command: `pip install -r requirements.txt`
3. Set start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
4. Add environment variables in the Render dashboard

**Production — Docker on any VPS:**
```bash
docker build -t smartsheet-claude-processor .
docker run -d -p 8000:8000 --env-file .env smartsheet-claude-processor
```

### 7. Register Smartsheet webhooks

Once your app is publicly reachable, run this **once**:

```bash
python register.py --url https://your-public-url.com/webhook
```

This registers and enables one webhook per sheet in `SHEET_IDS`. Your app handles the Smartsheet verification challenge automatically.

To delete all Claude processor webhooks and re-register from scratch:
```bash
python register.py --url https://your-public-url.com/webhook --reset
```

Webhook details are saved to `webhooks.json` for reference.

### 8. Test

Create a new row (with at least a task name filled in) in any watched sheet. Within 60–120 seconds you should see a `[Claude Research Note]` comment on that row.

To test comment follow-up: add any comment to an existing row that already has a Claude note. The processor will detect the new human activity and post an updated note.

Watch the app logs — row/cell events:
```
INFO  Queuing processor  sheet=5267249496543108  row=1234567890  objectType=cell
INFO  Processing  sheet=5267249496543108  row=1234567890
INFO  Claude finished  tools=['web_search', 'smartsheet_create_discussion_on_row']
INFO  Done  sheet=5267249496543108  row=1234567890  result=Posted [Claude Research Note]...
```

Comment/discussion events:
```
INFO  Queuing discussion resolver  sheet=5267249496543108  discussion=9876543210
INFO  Discussion 9876543210 resolved to row 1234567890 on sheet 5267249496543108
INFO  Comment event — waiting 5s for Smartsheet discussion propagation
INFO  Processing  sheet=5267249496543108  row=1234567890
```

---

## Skip rules

Claude will NOT post a comment if:

| Condition | Behaviour |
|---|---|
| Row `Status` = `Complete` | Skipped — task is done |
| Most recent comment starts with `[Claude Research Note]` | Skipped — Claude was last, no new human activity |
| Most recent comment starts with `[Skip]` | Skipped — human opted out |

**To leave a note without triggering Claude:** start your comment with `[Skip]`.

**To trigger a follow-up research pass:** reply to the row with any comment that does not start with `[Skip]`.

---

## Polling loop

In addition to real-time webhooks, the app polls all watched sheets every `POLL_INTERVAL_MINS` minutes. It only calls Claude for rows that have had **human activity** (cell edit or new human comment) within the last `2 × POLL_INTERVAL_MINS` minutes and don't already have a Claude comment as the most recent comment.

This catches rows whose webhooks were missed without re-processing old untouched rows.

---

## Backfill

To retroactively process existing rows that have never received a Claude comment:

```bash
# Dry run — shows what would be processed without calling Claude
python backfill.py

# Actually process (stop the main app first to avoid concurrent API calls)
python backfill.py --run
```

---

## Health check

```
GET /health
→ {"status": "ok", "sheets_watched": 8}
```

---

## Troubleshooting

**Webhooks show `DISABLED_VERIFICATION_FAILED`**
- Make sure the app is running *before* running `register.py`
- Your app must be reachable at the URL you pass to `register.py`
- Re-run `register.py` after confirming the app is up

**No comments appearing on rows**
- Check the app logs — every webhook arrival and processing step is logged
- Confirm `SHEET_IDS` in `.env` matches the sheet you're testing
- Confirm `ANTHROPIC_API_KEY` is valid and has credits
- Make sure the row has data in the Task Name column — blank rows are auto-deleted by Smartsheet

**Comments I add don't trigger a follow-up Claude note**
- Check the logs for `Queuing discussion resolver` lines when you add a comment
- If absent, the webhook isn't reaching the app — confirm ngrok is running and the tunnel URL matches the registered webhook URL
- If present but no Claude note appears, check for `SKIPPED` log lines (Claude may already be the last commenter)

**`Row not available after retries` in logs**
- This is usually a Smartsheet API propagation delay; the polling loop will pick it up within `POLL_INTERVAL_MINS` minutes
- If it keeps happening, increase retries in `fetch_row()` in `processor.py`

**`Discussion ... resolved to row` but no comment posted**
- Check for `SKIPPED` immediately after — the skip rules apply after the discussion is resolved
- Check for `Claude call failed` — may be an Anthropic API error (check key/credits)

**Claude posts but the comment content looks off**
- Check `SHEET_CLIENT_MAP` in `processor.py` — the sheet ID may be missing
- The Task Name (column 0) drives the research query; rows need a value in that column

**Credits being used unexpectedly**
- Check `POLL_INTERVAL_MINS` — lower values mean more frequent scans
- The polling loop only calls Claude for rows with recent activity, but webhooks fire on every row change across all watched sheets

---

## Architecture notes

- `main.py` — FastAPI app: webhook receiver, challenge handler, polling loop
- `processor.py` — core logic: row fetch with retry, discussion → row ID resolution, skip rules, prompt builder, Claude API call
- `config.py` — pydantic-settings config loaded from `.env`
- `register.py` — one-time CLI to register and enable Smartsheet webhooks
- `backfill.py` — one-time CLI to process existing rows without Claude comments

The Anthropic API call includes:
- `mcp_servers` — Smartsheet MCP at `https://mcp.smartsheet.com` (Claude fetches discussions and posts comments directly)
- `tools` — `web_search_20250305` (Claude searches for Qlik best practices relevant to the task)
- `anthropic-beta: mcp-client-2025-04-04` header required for MCP server support
