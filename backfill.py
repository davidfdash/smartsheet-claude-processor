"""
backfill.py
-----------
One-time script to process all existing rows that don't yet have a Claude comment.
Run while the app is NOT running (both use the same Anthropic/Smartsheet tokens).

    python backfill.py            # dry run — shows what would be processed
    python backfill.py --run      # actually calls Claude and posts comments
"""

import argparse
import time

import httpx

from config import settings
from processor import AI_TAG, SKIP_TAG, _flatten_comments, _ss_headers, SMARTSHEET_BASE, process_row_event

SHEET_CLIENT_MAP = {
    "5337282696400772": "Astellas",
    "5267249496543108": "Atria Senior Living",
    "901776017411972":  "Deloitte ITS",
    "8042391075245956": "Fitch Ratings",
    "6725738785886084": "HP Enterprise",
    "1818020853796740": "Johnson and Johnson",
    "5620664638590852": "Verizon",
    "2724886018477956": "Miscellaneous",
}


def fetch_sheet_rows(sheet_id: str) -> list[dict]:
    url = f"{SMARTSHEET_BASE}/sheets/{sheet_id}?include=discussions"
    with httpx.Client(timeout=30) as client:
        r = client.get(url, headers=_ss_headers())
        r.raise_for_status()
        return r.json().get("rows", [])


def row_needs_processing(row: dict) -> tuple[bool, str]:
    cells = row.get("cells", [])
    status = cells[2].get("value", "") if len(cells) > 2 else ""
    if status == "Complete":
        return False, "Status is Complete"

    comments = _flatten_comments(row)
    if comments:
        latest = comments[0].get("text", "")
        if latest.startswith(AI_TAG):
            return False, "Claude already commented last"
        if latest.startswith(SKIP_TAG):
            return False, "[Skip] tag present"

    return True, ""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", action="store_true", help="Actually process rows (default is dry run)")
    args = parser.parse_args()

    dry_run = not args.run
    if dry_run:
        print("DRY RUN — pass --run to actually process\n")

    for sheet_id in settings.sheet_ids:
        name = SHEET_CLIENT_MAP.get(sheet_id, sheet_id)
        print(f"\n=== {name} ({sheet_id}) ===")

        try:
            rows = fetch_sheet_rows(sheet_id)
        except Exception as e:
            print(f"  ERROR fetching sheet: {e}")
            continue

        for row in rows:
            row_id = str(row["id"])
            cells = row.get("cells", [])
            task = cells[0].get("value", "(blank)") if cells else "(blank)"
            needs, reason = row_needs_processing(row)

            if not needs:
                print(f"  SKIP  row={row_id}  reason={reason}  task={task!r}")
                continue

            print(f"  {'PROCESS' if not dry_run else 'WOULD PROCESS'}  row={row_id}  task={task!r}")
            if not dry_run:
                try:
                    process_row_event(sheet_id=sheet_id, row_id=row_id)
                except Exception as e:
                    print(f"    ERROR: {e}")
                time.sleep(2)  # avoid hammering the API


if __name__ == "__main__":
    main()
