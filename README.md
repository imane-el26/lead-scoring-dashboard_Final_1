# HubSpot Leads Dashboard — V2 Originaaaal

V2 adds historical Open Deal tracking and lifecycle-at-conversion analysis.

## What's new vs V1

- **`sticky.json` now contains conversion data**: for every contact that has ever
  been Open Deal, we know when (timestamp) and at which lifecycle stage they
  were at that exact moment.
- **Second chart shows the funnel**: "Open Deal conversions — by lifecycle at
  conversion" — answers the question "do our Open Deals come from Leads, MQLs,
  SQLs?"
- **One-shot backfill script** (`scripts/backfill_history.py`) walks every
  contact's property history and reconstructs the historical Open Deal passages
  that happened before V1 was even running.
- **Daily incremental tracking**: `fetch_leads.py` only queries the history of
  *new* Open Deal contacts since the last run, so it stays fast.
- **CSV export & modal** include the new `firstOpenDealAt` and
  `lifecycleAtConversion` fields.

## Quickstart (local testing)

```bash
# 1. Generate fake data and test the UI without hitting HubSpot
python3 scripts/test_local.py
python3 -m http.server 8000 --directory public
# Open http://localhost:8000

# 2. Real HubSpot data with backfill
export HUBSPOT_TOKEN="pat-na1-..."
python3 scripts/backfill_history.py    # one-shot, ~5-15 min
python3 scripts/fetch_leads.py         # builds leads.json from sticky.json

# 3. Inspect the result
python3 -m json.tool public/data/sticky.json | head -30
python3 -m http.server 8000 --directory public
```

## Architecture

Same as V1 (GitHub Actions cron + JSON in repo + Cloudflare Pages + Access),
plus the property-history layer for historical Open Deal reconstruction.

See `SETUP.md` for full deployment instructions.
