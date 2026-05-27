"""
register.py
-----------
One-time setup script that registers and enables a Smartsheet webhook
for every sheet ID in your .env file.

Usage:
    python register.py --url https://your-public-url.com/webhook

The --url argument is the public URL where your FastAPI app is reachable.
Run this AFTER the app is deployed and publicly accessible.

To delete all webhooks and start fresh:
    python register.py --url https://your-public-url.com/webhook --reset
"""

import argparse
import json
import sys

import httpx

from config import settings

SMARTSHEET_BASE = "https://api.smartsheet.com/2.0"


def ss_headers() -> dict:
    return {
        "Authorization": f"Bearer {settings.smartsheet_token}",
        "Content-Type": "application/json",
    }


def list_webhooks() -> list[dict]:
    with httpx.Client(timeout=30) as client:
        r = client.get(f"{SMARTSHEET_BASE}/webhooks", headers=ss_headers())
        r.raise_for_status()
        return r.json().get("data", [])


def delete_webhook(webhook_id: int) -> None:
    with httpx.Client(timeout=30) as client:
        r = client.delete(
            f"{SMARTSHEET_BASE}/webhooks/{webhook_id}",
            headers=ss_headers(),
        )
        r.raise_for_status()


def register_webhook(sheet_id: str, callback_url: str, name: str) -> dict:
    payload = {
        "name": name,
        "callbackUrl": callback_url,
        "scope": "sheet",
        "scopeObjectId": int(sheet_id),
        "events": ["*.*"],
        "version": 1,
    }
    with httpx.Client(timeout=30) as client:
        r = client.post(
            f"{SMARTSHEET_BASE}/webhooks",
            headers=ss_headers(),
            json=payload,
        )
        r.raise_for_status()
        return r.json().get("result", {})


def enable_webhook(webhook_id: int) -> dict:
    with httpx.Client(timeout=30) as client:
        r = client.put(
            f"{SMARTSHEET_BASE}/webhooks/{webhook_id}",
            headers=ss_headers(),
            json={"enabled": True},
        )
        r.raise_for_status()
        return r.json().get("result", {})


def get_webhook_status(webhook_id: int) -> dict:
    with httpx.Client(timeout=30) as client:
        r = client.get(
            f"{SMARTSHEET_BASE}/webhooks/{webhook_id}",
            headers=ss_headers(),
        )
        r.raise_for_status()
        return r.json().get("result", {})


def main():
    parser = argparse.ArgumentParser(
        description="Register and enable Smartsheet webhooks for the Claude processor"
    )
    parser.add_argument(
        "--url",
        required=True,
        help="Public URL of your running FastAPI app, e.g. https://abc.ngrok.io/webhook",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Delete ALL existing Claude processor webhooks before registering new ones",
    )
    args = parser.parse_args()

    callback_url = args.url.rstrip("/")
    if not callback_url.endswith("/webhook"):
        callback_url = f"{callback_url}/webhook"

    print(f"\nCallback URL: {callback_url}")
    print(f"Sheet IDs:    {settings.sheet_ids}\n")

    if not settings.sheet_ids:
        print("ERROR: No SHEET_IDS found in .env — add them and try again.")
        sys.exit(1)

    # ── Optional reset ────────────────────────────────────────────────────
    if args.reset:
        print("-- Reset: deleting existing Claude processor webhooks --")
        existing = list_webhooks()
        deleted = 0
        for wh in existing:
            if "Claude AI Processor" in wh.get("name", ""):
                try:
                    delete_webhook(wh["id"])
                    print(f"  Deleted: {wh['name']} ({wh['id']})")
                    deleted += 1
                except Exception as e:
                    print(f"  Failed to delete {wh['id']}: {e}")
        print(f"  {deleted} webhook(s) deleted\n")

    # ── Register + enable ─────────────────────────────────────────────────
    results = []
    for sheet_id in settings.sheet_ids:
        name = f"Claude AI Processor - {sheet_id}"
        print(f"Registering sheet {sheet_id}...")

        try:
            wh = register_webhook(sheet_id, callback_url, name)
            wh_id = wh["id"]
            print(f"  Created   ID={wh_id}  status={wh.get('status')}")
        except Exception as e:
            print(f"  ERROR registering: {e}")
            continue

        try:
            enabled = enable_webhook(wh_id)
            status  = enabled.get("status")
            print(f"  Enabled   status={status}")
            results.append({"sheet_id": sheet_id, "webhook_id": wh_id, "status": status})
        except Exception as e:
            print(f"  ERROR enabling: {e}")
            results.append({"sheet_id": sheet_id, "webhook_id": wh_id, "status": "ENABLE_FAILED"})

    # ── Summary ───────────────────────────────────────────────────────────
    print("\n-- Summary --------------------------------------------------")
    all_enabled = True
    for r in results:
        icon   = "✓" if r["status"] == "ENABLED" else "✗"
        print(f"  {icon}  sheet={r['sheet_id']}  webhook={r['webhook_id']}  status={r['status']}")
        if r["status"] != "ENABLED":
            all_enabled = False

    if all_enabled:
        print("\nAll webhooks ENABLED. Create a row in any watched sheet to test!")
    else:
        print(
            "\nSome webhooks failed verification. Make sure your app is publicly "
            "reachable at the URL above and try running this script again."
        )

    # Save webhook IDs to a local file for reference
    with open("webhooks.json", "w") as f:
        json.dump(results, f, indent=2)
    print("\nWebhook details saved to webhooks.json")


if __name__ == "__main__":
    main()
