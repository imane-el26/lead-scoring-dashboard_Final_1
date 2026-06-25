"""
One-shot backfill: walk through EVERY contact in the portal and check their
hs_lead_status history. For any contact that has EVER been OPEN_DEAL (even
if they aren't right now), capture:
  - first time they reached OPEN_DEAL
  - lifecyclestage active at that moment

Then writes the result to public/data/sticky.json.

Run this ONCE manually before relying on the daily fetch_leads.py.
The daily script will keep the data fresh after that.

Expected duration: ~5-15 minutes for 50k contacts (one API call per contact
plus batched listing). Includes 429 backoff and gentle pacing.

Usage:
  export HUBSPOT_TOKEN="pat-na1-..."
  python3 scripts/backfill_history.py
"""

import json
import os
import sys
import time
from urllib import request, parse, error

HUBSPOT_API = "https://api.hubapi.com"
TOKEN = os.environ.get("HUBSPOT_TOKEN", "")
STICKY_PATH = "public/data/sticky.json"


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
            raise
    raise RuntimeError("HubSpot API gave up after 6 attempts: " + url)


def fetch_all_contact_ids():
    """List every contact id in the portal. We only need IDs + current status here."""
    print("Listing every contact in the portal...")
    ids = []
    statuses = {}  # id -> current hs_lead_status (to skip the obvious cases)
    after = None
    while True:
        params = {
            "limit": "100",
            "properties": "hs_lead_status",
        }
        if after:
            params["after"] = after
        data = hubspot_request("/crm/v3/objects/contacts", params=params)
        for c in data.get("results", []):
            cid = str(c.get("id"))
            ids.append(cid)
            statuses[cid] = (c.get("properties") or {}).get("hs_lead_status") or ""
        if len(ids) % 1000 == 0 and len(ids) > 0:
            print("  listed " + str(len(ids)) + " contacts so far...")
        paging = data.get("paging", {}).get("next", {})
        after = paging.get("after")
        if not after:
            break
    print("Listed " + str(len(ids)) + " contacts.")
    return ids, statuses


def get_contact_history(contact_id, props):
    params = {
        "propertiesWithHistory": ",".join(props),
        "archived": "false",
    }
    data = hubspot_request("/crm/v3/objects/contacts/" + str(contact_id), params=params)
    history_raw = data.get("propertiesWithHistory") or {}
    out = {}
    for prop in props:
        entries = history_raw.get(prop) or []
        out[prop] = [
            {"value": e.get("value"), "timestamp": e.get("timestamp")}
            for e in entries
        ]
    return out


def first_open_deal_conversion(history):
    status_history = history.get("hs_lead_status") or []
    lifecycle_history = history.get("lifecyclestage") or []
    open_deal_entries = [e for e in status_history if e.get("value") == "OPEN_DEAL"]
    if not open_deal_entries:
        return None
    earliest = min(open_deal_entries, key=lambda e: e.get("timestamp") or "")
    open_deal_at = earliest.get("timestamp")
    if not open_deal_at:
        return None

    lifecycle_at = None
    sorted_lc = sorted(lifecycle_history, key=lambda e: e.get("timestamp") or "")
    for entry in sorted_lc:
        ts = entry.get("timestamp")
        # Use strict < (not <=) because HubSpot often auto-syncs lifecyclestage
        # to "opportunity" at the EXACT same timestamp as hs_lead_status moving
        # to OPEN_DEAL. We want the stage from BEFORE that moment, not the
        # auto-synced opportunity that arrives in the same instant.
        if ts and ts < open_deal_at:
            lifecycle_at = entry.get("value")
        else:
            break
    if not lifecycle_at and sorted_lc:
        lifecycle_at = sorted_lc[0].get("value")

    return {
        "firstOpenDealAt": open_deal_at,
        "lifecycleAtConversion": lifecycle_at or "",
    }


def lifecycle_passages(history):
    """Look through the lifecyclestage history and return a set of every
    distinct stage the contact has ever been at."""
    lifecycle_history = history.get("lifecyclestage") or []
    stages = set()
    for entry in lifecycle_history:
        v = entry.get("value")
        if v:
            stages.add(v)
    return stages


def main():
    contact_ids, statuses = fetch_all_contact_ids()
    print()
    print("Now scanning property history for each contact to find historical")
    print("Open Deal passages AND lifecycle stage history. This may take 10-15 minutes...")
    print()

    open_deal_ids = set()
    conversions = {}
    # Contact IDs grouped by whether they were ever at a given lifecycle stage
    ever_lead_ids = set()           # ever had lifecyclestage = lead
    ever_mql_ids = set()            # ever had lifecyclestage = marketingqualifiedlead
    ever_sql_ids = set()            # ever had lifecyclestage = salesqualifiedlead
    ever_subscriber_ids = set()
    ever_opportunity_ids = set()
    ever_customer_ids = set()
    errors = 0

    for i, cid in enumerate(contact_ids):
        try:
            history = get_contact_history(cid, ["hs_lead_status", "lifecyclestage"])

            # Open Deal conversion (with lifecycle-at-conversion)
            conv = first_open_deal_conversion(history)
            if conv:
                open_deal_ids.add(cid)
                conversions[cid] = conv

            # Lifecycle passages — record every stage this contact ever had
            stages = lifecycle_passages(history)
            if "lead" in stages: ever_lead_ids.add(cid)
            if "marketingqualifiedlead" in stages: ever_mql_ids.add(cid)
            if "salesqualifiedlead" in stages: ever_sql_ids.add(cid)
            if "subscriber" in stages: ever_subscriber_ids.add(cid)
            if "opportunity" in stages: ever_opportunity_ids.add(cid)
            if "customer" in stages: ever_customer_ids.add(cid)

        except Exception as e:
            errors += 1
            if errors <= 5:
                print("  warn: contact " + cid + ": " + str(e))

        # Progress + gentle pacing
        if (i + 1) % 100 == 0:
            print("  processed " + str(i + 1) + "/" + str(len(contact_ids))
                  + " | open-deal: " + str(len(open_deal_ids))
                  + " | ever-lead: " + str(len(ever_lead_ids))
                  + " | ever-mql: " + str(len(ever_mql_ids))
                  + " | errors: " + str(errors))
        time.sleep(0.05)  # small pause to avoid hammering the API

    sticky_out = {
        "openDealIds": sorted(open_deal_ids),
        "conversions": conversions,
        # Sets of contact IDs that ever passed through each lifecycle stage.
        # Lets the dashboard answer: "of every contact who ever was at MQL,
        # how many ever became Open Deal?" — the analysis the team wants.
        "everLeadIds": sorted(ever_lead_ids),
        "everMqlIds": sorted(ever_mql_ids),
        "everSqlIds": sorted(ever_sql_ids),
        "everSubscriberIds": sorted(ever_subscriber_ids),
        "everOpportunityIds": sorted(ever_opportunity_ids),
        "everCustomerIds": sorted(ever_customer_ids),
    }
    with open(STICKY_PATH, "w", encoding="utf-8") as f:
        json.dump(sticky_out, f, ensure_ascii=False, indent=2)

    print()
    print("=" * 60)
    print("Backfill complete.")
    print("  Total contacts scanned:  " + str(len(contact_ids)))
    print("  Historically Open Deal:  " + str(len(open_deal_ids)))
    print("  Ever Lead:               " + str(len(ever_lead_ids)))
    print("  Ever MQL:                " + str(len(ever_mql_ids)))
    print("  Ever SQL:                " + str(len(ever_sql_ids)))
    print("  Ever Opportunity:        " + str(len(ever_opportunity_ids)))
    print("  Ever Customer:           " + str(len(ever_customer_ids)))
    print("  Errors (skipped):        " + str(errors))
    print("  Output:                  " + STICKY_PATH)
    print("=" * 60)
    print()
    print("Next steps:")
    print("  1. Run: python3 scripts/fetch_leads.py    (builds leads.json from the new sticky)")
    print("  2. git add public/data/")
    print("  3. git commit -m 'Backfill historical lifecycle passages'")
    print("  4. git push")


if __name__ == "__main__":
    main()
