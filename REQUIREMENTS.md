# Requirements: AI-Powered Task Advisor for Qlik Consulting Project Boards

**Project:** Smartsheet Claude AI Processor  
**Repository:** https://github.com/davidfdash/smartsheet-claude-processor  
**Status:** Draft  
**Date:** 2026-06-04

---

## 1. Background

A Qlik consulting practice manages multiple concurrent client engagements using Smartsheet project boards — one sheet per client. Each row on a board represents a discrete consulting task or deliverable (referred to as a "card"). Consultants create, update, and comment on cards as work progresses.

Currently, when a consultant updates a card, they must manually research next steps, cross-reference open Salesforce support cases, and surface relevant Qlik best practices. This is time-consuming, inconsistent across team members, and often skipped under delivery pressure.

This document specifies requirements for an automated AI advisor that monitors Smartsheet project boards in real time and posts researched, contextually-aware next-step recommendations as comments on cards — drawing on the card's own data, recent discussion history, live Salesforce case information for the client, and current Qlik best practices sourced from the web.

---

## 2. Goals

| Goal | Description |
|---|---|
| **G-1** | Reduce time consultants spend manually researching next steps per card |
| **G-2** | Ensure every active card has an up-to-date AI research note before the next client touchpoint |
| **G-3** | Surface open Salesforce support cases relevant to each card so consultants coordinate with the support team |
| **G-4** | Apply consistent research quality across all consultants and clients |
| **G-5** | Allow consultants to suppress or redirect AI activity on specific cards |

---

## 3. Stakeholders

| Role | Responsibility |
|---|---|
| Consulting team | Consumes AI research notes; triggers follow-up passes by commenting |
| Practice lead | Configures which sheets are monitored; manages API credentials |
| Salesforce support team | Owns case data that is read (not modified) by the system |
| System host | Runs and maintains the self-hosted application (one instance per practice) |

---

## 4. System Overview

The system is a self-hosted web application that:

1. Receives real-time event notifications from Smartsheet whenever a card is created, updated, or commented on
2. Retrieves the full card context (task details, discussion history)
3. Retrieves open Salesforce support cases for the relevant client
4. Instructs an AI model (Anthropic Claude) to research the task, cross-reference cases, and produce a structured next-steps recommendation
5. Posts that recommendation as a comment back on the Smartsheet card

A periodic polling loop provides a safety net for any events missed by the real-time notification path.

```
Consultant creates / updates / comments on a Smartsheet card
                        │
                        ▼
          ┌─────────────────────────┐
          │   Webhook receiver      │  ◄── Smartsheet sends POST within seconds
          │   (FastAPI /webhook)    │
          └────────────┬────────────┘
                       │  Returns 200 immediately
                       │
                       ▼
          ┌─────────────────────────┐
          │   Background processor  │
          └────────────┬────────────┘
                       │
           ┌───────────┼───────────┐
           ▼           ▼           ▼
     Smartsheet    Salesforce    Skip
     REST API      REST API      rules
     (card data)   (open cases)
           │           │
           └─────┬─────┘
                 ▼
         Anthropic Claude API
         + Smartsheet MCP tools
         + web_search tool
                 │
                 ▼
        [Claude Research Note]
        posted as card comment
```

---

## 5. Functional Requirements

### 5.1 Event ingestion

**FR-1.1** The system must expose a single HTTPS webhook endpoint (`POST /webhook`) reachable from the public internet.

**FR-1.2** The endpoint must respond with HTTP 200 within 5 seconds of receiving any Smartsheet request. All processing must occur asynchronously after the 200 is returned.

**FR-1.3** The system must handle the Smartsheet webhook verification challenge: when Smartsheet POSTs `{"challenge": "<value>"}`, the system must respond synchronously with `{"smartsheetHookResponse": "<value>"}`.

**FR-1.4** The system must process events of type `row.created`, `row.updated`, `cell.updated`, and `comment.created`.

**FR-1.5** When multiple events arrive for the same card in a single webhook callback, the system must process that card only once (deduplication by row ID per callback).

**FR-1.6** The system must ignore events for sheet IDs not in the configured watch list.

### 5.2 Card eligibility (skip rules)

Before calling the AI, the system must check whether a card qualifies for a research note. A card must be **skipped** if any of the following are true:

| Condition | Reason |
|---|---|
| Card `Status` column value = `Complete` | Task is finished; no next steps needed |
| Most recent comment begins with `[Claude Research Note]` | AI was last to comment; no new human activity has occurred |
| Most recent comment begins with `[Skip]` | Consultant has explicitly opted the card out |

If a card is skipped, the system must log the reason and take no further action on that card for that event.

### 5.3 Salesforce case retrieval

**FR-3.1** For each card that passes the skip check, the system must retrieve open Salesforce Cases associated with the client account linked to that card's sheet.

**FR-3.2** The client-to-Salesforce Account mapping must be configurable without code changes (see §6.3).

**FR-3.3** Only Cases where `IsClosed = false` must be retrieved.

**FR-3.4** The system must retrieve the following fields per Case:

| Field | Purpose |
|---|---|
| `CaseNumber` | Human-readable reference for the consultant |
| `Subject` | One-line description of the issue |
| `Status` | Open / Escalated / Pending Customer / etc. |
| `Priority` | Low / Medium / High / Critical |
| `CreatedDate` | Age of the case |
| `LastModifiedDate` | Recency of activity |
| `Description` | Issue detail (truncated to 500 characters in prompt) |
| `Owner.Name` | Salesforce support case owner |

**FR-3.5** Results must be sorted by `Priority` descending, then `LastModifiedDate` descending.

**FR-3.6** A maximum of 10 cases must be passed to the AI. If more exist, the prompt must note the total count so the consultant knows to check Salesforce directly.

**FR-3.7** If the Salesforce lookup returns no cases, the AI prompt must state this explicitly so Claude does not assume data is missing.

**FR-3.8** If the Salesforce API call fails for any reason (authentication error, network timeout, account not mapped), the system must:
- Log the failure at WARNING level (without logging credentials or full case data)
- Continue processing the card with the Smartsheet-only context
- Include a notice in the prompt that Salesforce data is unavailable

**FR-3.9** The system must not create, update, delete, or close any Salesforce record.

### 5.4 AI research and comment

**FR-4.1** The system must pass the following context to the AI model in a structured prompt:

- Sheet ID and Row ID (for tool use)
- Client name
- Task name, owner, status, priority, due date, and notes
- Up to 5 most recent card comments (newest first)
- Whether a prior AI research note exists on this card
- All open Salesforce cases retrieved in §5.3

**FR-4.2** The AI must re-fetch the card's live discussion state via the Smartsheet MCP before posting, to confirm no human has commented since the background task was queued.

**FR-4.3** The AI must apply the same skip rules as §5.2 based on the freshly fetched state.

**FR-4.4** The AI must perform a web search for current Qlik best practices relevant to the task name and client.

**FR-4.5** If a prior AI note exists on the card, the AI must focus its research on what has changed since that note, based on the most recent human comments.

**FR-4.6** The AI must post a comment on the card using the Smartsheet MCP. The comment must follow this structure (under 400 words, Markdown):

```
[Claude Research Note]

🤖 **AI Research & Next Steps**

*[1–2 sentence summary contextualising the task for the client and technology.]*

**Open Salesforce cases (relevant to this task):**
- SF-XXXXXXXX [Priority / Status] — Subject line
- SF-XXXXXXXX [Priority / Status] — Subject line
(Omit this section entirely if no cases are relevant)

**Suggested next steps:**
- [Concrete, actionable step]
- [Concrete, actionable step]
- [Concrete, actionable step]

---
*Reply to this comment to trigger a follow-up research pass.*
*Start your reply with [Skip] to leave a note without triggering Claude.*
```

**FR-4.7** The AI must not hallucinate case numbers. It may only reference `CaseNumber` values explicitly provided in the prompt.

**FR-4.8** After posting, the AI must respond with a single confirmation line: `Posted [Claude Research Note] to [ClientName] 'TaskName'` or `SKIPPED: <reason>`.

### 5.5 Polling loop

**FR-5.1** In addition to real-time webhook processing, the system must run a background polling loop that periodically scans all watched sheets for cards that may have been missed.

**FR-5.2** The polling interval must be configurable (default: 10 minutes).

**FR-5.3** The polling loop must only consider cards that have had **human-driven activity** (cell change or new human comment) within the last `2 × poll_interval` minutes. Cards with no recent activity must not be processed, to avoid unnecessary AI API calls.

**FR-5.4** The polling loop must apply all skip rules (§5.2) before queuing any card.

**FR-5.5** If both the webhook and the polling loop identify the same card simultaneously, the card must be processed only once (in-flight deduplication).

### 5.6 Webhook registration

**FR-6.1** The system must include a command-line script (`register.py`) that registers and enables one Smartsheet webhook per configured sheet ID.

**FR-6.2** The script must accept a `--url` argument specifying the public webhook endpoint.

**FR-6.3** The script must accept a `--reset` flag that deletes all existing Claude processor webhooks before re-registering.

**FR-6.4** The script must report registration and verification status for each sheet.

### 5.7 Backfill

**FR-7.1** The system must include a command-line script (`backfill.py`) that processes all existing cards across all watched sheets that have never received an AI research note.

**FR-7.2** The script must support a `--dry-run` mode (the default) that lists qualifying cards without calling the AI.

**FR-7.3** The script must support a `--run` flag to execute the actual processing.

---

## 6. Configuration Requirements

### 6.1 Environment variables

All configuration must be provided via environment variables (`.env` file). No credentials or client-specific data may be hardcoded.

| Variable | Required | Description |
|---|---|---|
| `SMARTSHEET_TOKEN` | ✅ | Smartsheet API personal access token |
| `ANTHROPIC_API_KEY` | ✅ | Anthropic API key |
| `SHEET_IDS` | ✅ | Comma-separated Smartsheet sheet IDs to watch |
| `SALESFORCE_INSTANCE_URL` | ✅ | e.g. `https://myorg.my.salesforce.com` |
| `SALESFORCE_CLIENT_ID` | ✅ | Salesforce Connected App consumer key |
| `SALESFORCE_CLIENT_SECRET` | ✅ | Salesforce Connected App consumer secret |
| `SALESFORCE_USERNAME` | ✅ | API service account username |
| `SALESFORCE_PASSWORD` | ✅ | Service account password + security token |
| `CLAUDE_MODEL` | | AI model (default: `claude-sonnet-4-6`) |
| `ANTHROPIC_TIMEOUT` | | Claude timeout in seconds (default: `180`) |
| `POLL_INTERVAL_MINS` | | Polling loop interval (default: `10`) |
| `PORT` | | Server port (default: `8000`) |
| `HOST` | | Bind address (default: `0.0.0.0`) |

### 6.2 Sheet-to-client mapping

A dictionary in `processor.py` (`SHEET_CLIENT_MAP`) maps sheet IDs to human-readable client names. The client name is used in the AI prompt and web search query.

### 6.3 Sheet-to-Salesforce account mapping

A dictionary in `processor.py` (`SHEET_SALESFORCE_ACCOUNT_MAP`) maps sheet IDs to Salesforce Account IDs. Sheets without a mapping produce an AI note without Salesforce context (with a notice in the comment).

Both maps must be documented clearly so a non-developer can update them.

---

## 7. Non-Functional Requirements

| Requirement | Target |
|---|---|
| Webhook response time | < 5 seconds (Smartsheet hard requirement) |
| End-to-end processing time (webhook → comment posted) | ≤ 3 minutes |
| Salesforce API call latency | < 5 seconds |
| Concurrent card processing | Unlimited (each card runs as an independent background task) |
| Duplicate processing prevention | In-flight set prevents concurrent processing of the same card |
| Availability | Best-effort; no SLA (self-hosted, single instance) |
| Credential handling | Credentials never logged; `.env` excluded from version control |
| Salesforce data logging | Only case count logged (not subjects, descriptions, or case numbers) |

---

## 8. Technical Constraints

- **Language / runtime:** Python 3.11+
- **Web framework:** FastAPI with uvicorn
- **HTTP client:** httpx (used for all outbound API calls)
- **AI API:** Anthropic Messages API (`POST /v1/messages`) with `anthropic-beta: mcp-client-2025-04-04` header
- **AI tools:** Smartsheet MCP server (`https://mcp.smartsheet.com`) and `web_search_20250305` built-in tool
- **Salesforce API:** Salesforce REST API v57.0+ with OAuth 2.0 Username-Password flow; SOQL for case queries
- **Deployment:** Must support running locally (with ngrok for tunnel), Docker, and PaaS platforms (Railway, Render)
- **No new external dependencies** for Salesforce integration (httpx is already present)

---

## 9. Out of Scope

- Writing to Salesforce (creating, updating, or closing cases)
- Syncing Smartsheet row status back to Salesforce
- Fetching Salesforce Opportunities, Contacts, or objects other than Cases
- Multi-Salesforce-org support
- End-user (per-consultant) OAuth to Salesforce
- A web-based UI for configuration or history
- Alerting or escalation beyond the Smartsheet comment

---

## 10. Open Questions

| # | Question | Owner |
|---|---|---|
| OQ-1 | Which Salesforce org (production / sandbox) for initial testing? | Dave |
| OQ-2 | Does a Salesforce Connected App already exist, or does one need to be created? | Dave / Salesforce admin |
| OQ-3 | Should `SHEET_SALESFORCE_ACCOUNT_MAP` be seeded for all current clients immediately, or phased? | Dave |
| OQ-4 | Are there clients in `SHEET_CLIENT_MAP` with no Salesforce account? How should those be handled? | Dave |
| OQ-5 | Should cases from child accounts (Salesforce account hierarchy) be included? | Dave |
| OQ-6 | Is the 10-case cap appropriate, or should it be configurable? | Dave |

---

## 11. Acceptance Criteria

- [ ] A card updated for a client with open Salesforce cases produces an AI comment referencing at least one case number
- [ ] A card updated for a client with no open cases produces a normal AI comment with no Salesforce section
- [ ] A Salesforce API failure does not prevent the AI comment from being posted; the comment notes that Salesforce data was unavailable
- [ ] A card with `Status = Complete` produces no AI comment
- [ ] A card whose most recent comment is an AI research note produces no AI comment (skip — no new human activity)
- [ ] A card whose most recent comment starts with `[Skip]` produces no AI comment
- [ ] Adding a non-`[Skip]` comment to a card that already has an AI note triggers a follow-up research pass
- [ ] The polling loop processes a card missed by the webhook within `POLL_INTERVAL_MINS` minutes, provided the card had recent human activity
- [ ] All Salesforce and Anthropic credentials are stored only in `.env` and never appear in logs or version control
- [ ] The application starts, registers webhooks, and processes a test card end-to-end in under 30 minutes from a fresh clone of the repository

---

## 12. References

- **Repository:** https://github.com/davidfdash/smartsheet-claude-processor
- **Smartsheet Webhook API:** https://smartsheet.com/developers/api-docs#webhooks
- **Smartsheet MCP Server:** https://mcp.smartsheet.com
- **Anthropic Messages API:** https://docs.anthropic.com/en/api/messages
- **Anthropic MCP client docs:** https://docs.anthropic.com/en/docs/agents-and-tools/mcp
- **Salesforce REST API:** https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta
- **Salesforce SOQL reference:** https://developer.salesforce.com/docs/atlas.en-us.soql_sosl.meta/soql_sosl
- **Salesforce Connected Apps:** https://help.salesforce.com/s/articleView?id=sf.connected_app_overview.htm
