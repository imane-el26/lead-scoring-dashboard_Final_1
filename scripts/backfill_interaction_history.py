"""
One-shot backfill: for every contact already known to have converted to
Open Deal (public/data/sticky.json -> conversions), fetch the HubSpot change
history of the "interaction signal" properties below and compute what each
one's value was at the EXACT moment the contact first became Open Deal.

Why: fetch_leads.py only computes this going forward, for NEW Open Deal
conversions it detects on each daily run (see INTERACTION_HISTORY_PROPERTIES
in fetch_leads.py). Contacts that converted before that feature shipped are
missing `interactionAtConversion` in sticky.json — this script backfills them
using the firstOpenDealAt timestamp sticky.json already has for each one, so
no re-detection of the conversion itself is needed.

Run this ONCE. After that, fetch_leads.py keeps new conversions up to date
automatically.

HubSpot's batch/read endpoint documents a 100-input cap, but that cap is
lower — undocumented, and apparently portal-dependent — once
`propertiesWithHistory` is in the request body (confirmed empirically: every
100-sized batch here came back HTTP 400, while a leftover 5-sized batch
succeeded). Rather than hardcode a guessed number, this script starts at 100
and halves the batch size automatically whenever HubSpot rejects one, so it
finds the real ceiling for this portal on its own and remembers it for the
rest of the run.

Usage:
  export HUBSPOT_TOKEN="pat-na1-..."
  python3 scripts/backfill_interaction_history.py
"""

import json
import os
import sys
import time
from urllib import request, parse, error

HUBSPOT_API = "https://api.hubapi.com"
TOKEN = os.environ.get("HUBSPOT_TOKEN", "")
STICKY_PATH = "public/data/sticky.json"

# Keep in sync with INTERACTION_HISTORY_PROPERTIES in fetch_leads.py.
INTERACTION_HISTORY_PROPERTIES = [
    "meeting_type_cp__c",
    "num_conversion_events",
    "hs_analytics_last_url",
    "message",
    "enrolled_ai_referrals",
    "hs_email_click",
    "hs_email_open",
    "hs_google_click_id",
    "hs_email_optout",
    "last_webinar_seen",
    "origine_event",
]


def hubspot_request(path, method="GET", body=None, params=None):
    if not TOKEN:
        raise RuntimeError("HUBSPOT_TOKEN env var is empty. Set it before running this script.")
    url = HUBSPOT_API + path
    if params:
        url += "?" + parse.urlencode(params)
    headers = {
        "Authorization": "Bearer " + TOKEN,
        "Content-Type": "application/json",
    }
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = request.Request(url, data=data, method=method, headers=headers)
    for attempt in range(6):
        try:
            with request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except error.HTTPError as e:
            if e.code == 429:
                wait = 2 ** attempt
                sys.stdout.write("  [429] sleeping " + str(wait) + "s... ")
                sys.stdout.flush()
                time.sleep(wait)
                continue
            # Surface HubSpot's actual error body (not just "Bad Request") so
            # a real problem (bad property name, permissions...) is visible
            # instead of being indistinguishable from a batch-size cap.
            try:
                detail = e.read().decode("utf-8")
            except Exception:
                detail = ""
            raise RuntimeError("HTTP " + str(e.code) + ": " + detail[:500]) from e
    raise RuntimeError("HubSpot API gave up after 6 attempts: " + url)


def value_at_timestamp(entries, target_ts):
    """Same logic as fetch_leads.py's value_at_timestamp — find the value
    that was active at target_ts (most recent entry with timestamp <=
    target_ts), falling back to the earliest known entry."""
    if not entries or not target_ts:
        return None
    sorted_entries = sorted(entries, key=lambda e: e.get("timestamp") or "")
    value = None
    for entry in sorted_entries:
        ts = entry.get("timestamp")
        if ts and ts <= target_ts:
            value = entry.get("value")
        else:
            break
    if value is None:
        value = sorted_entries[0].get("value")
    return value


def fetch_history_batch(chunk):
    """chunk = list of (cid, target_ts). Returns {cid: interactionAtConversion
    dict}. Raises on any HubSpot error (400, auth, etc.) — caller decides
    whether to split and retry."""
    chunk_ids = [cid for cid, _ in chunk]
    body = {
        "inputs": [{"id": cid} for cid in chunk_ids],
        "propertiesWithHistory": INTERACTION_HISTORY_PROPERTIES,
    }
    data = hubspot_request("/crm/v3/objects/contacts/batch/read", method="POST", body=body)
    target_ts_by_id = dict(chunk)
    out = {}
    for r in data.get("results", []):
        cid = str(r.get("id"))
        target_ts = target_ts_by_id.get(cid)
        if not target_ts:
            continue
        history_raw = r.get("propertiesWithHistory") or {}
        out[cid] = {
            prop: value_at_timestamp(history_raw.get(prop) or [], target_ts)
            for prop in INTERACTION_HISTORY_PROPERTIES
        }
    return out


def process_chunk(chunk, results, error_details):
    """Try to fetch history for `chunk`. On failure, split in half and retry
    each half recursively (down to individual contacts) — this is what
    discovers HubSpot's real batch-size ceiling for this portal. Mutates
    `results` (cid -> interactionAtConversion) and `error_details`
    (list of (cid, error message)) in place. Returns the batch size that
    actually worked (smallest successful call so far), or None if even
    single-contact calls are failing.
    """
    try:
        results.update(fetch_history_batch(chunk))
        return len(chunk)
    except Exception as e:
        if len(chunk) == 1:
            error_details.append((chunk[0][0], str(e)))
            return None
        mid = len(chunk) // 2
        time.sleep(0.05)
        left = process_chunk(chunk[:mid], results, error_details)
        right = process_chunk(chunk[mid:], results, error_details)
        sizes = [s for s in (left, right) if s]
        return min(sizes) if sizes else None


def load_sticky():
    with open(STICKY_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def write_sticky(sticky):
    with open(STICKY_PATH, "w", encoding="utf-8") as f:
        json.dump(sticky, f, ensure_ascii=False, indent=2)


def main():
    sticky = load_sticky()
    conversions = sticky.get("conversions") or {}

    todo = [
        (cid, conv["firstOpenDealAt"])
        for cid, conv in conversions.items()
        if conv.get("firstOpenDealAt") and "interactionAtConversion" not in conv
    ]
    print("Conversions needing backfill: " + str(len(todo)) + " / " + str(len(conversions)))
    if not todo:
        print("Nothing to do — every conversion already has interactionAtConversion.")
        return

    results = {}
    error_details = []
    batch_size = 100
    i = 0
    while i < len(todo):
        chunk = todo[i:i + batch_size]
        worked_size = process_chunk(chunk, results, error_details)
        if worked_size and worked_size < batch_size:
            print("  HubSpot rejected batches of " + str(batch_size)
                  + " — continuing with batches of " + str(worked_size))
            batch_size = worked_size
        i += len(chunk)
        print("  processed " + str(min(i, len(todo))) + "/" + str(len(todo))
              + " | done: " + str(len(results)) + " | errors: " + str(len(error_details)))
        time.sleep(0.2)

    for cid, interaction in results.items():
        conversions[cid]["interactionAtConversion"] = interaction

    sticky["conversions"] = conversions
    write_sticky(sticky)

    print()
    print("=" * 60)
    print("Backfill complete.")
    print("  Conversions backfilled: " + str(len(results)))
    print("  Errors (skipped):       " + str(len(error_details)))
    if error_details:
        print("  First few errors:")
        for cid, msg in error_details[:5]:
            print("    " + cid + ": " + msg[:200])
    print("  Output:                 " + STICKY_PATH)
    print("=" * 60)
    print()
    print("Next steps:")
    print("  1. Run: python3 scripts/fetch_leads.py    (rebuilds leads.json with interactionAtConversion)")
    print("  2. git add public/data/")
    print("  3. git commit -m 'Backfill interaction-at-conversion history'")
    print("  4. git push")


if __name__ == "__main__":
    main()
