"""
Fetch contacts from HubSpot and write public/data/leads.json + sticky.json.

This script is run daily by GitHub Actions. It:
  1. Fetches the current snapshot of all contacts (paginated).
  2. Reads the existing sticky.json (cumulative Open Deal IDs + lifecycle-at-conversion data).
  3. For any contact whose current hs_lead_status == OPEN_DEAL and is NOT already
     in the sticky set, fetches their property history to find out:
       - the date of their FIRST passage to OPEN_DEAL
       - the lifecyclestage they had at that exact moment
     Then adds them to sticky.json with that conversion data.
  4. Writes leads.json with each contact annotated with `everOpenDeal` + (if applicable)
     `firstOpenDealAt` and `lifecycleAtConversion`.

For the initial backfill (when sticky.json has very few historical entries), run
`backfill_history.py` manually first. After that, this daily script handles new
conversions incrementally.
"""

import json
import os
import sys
import time
from datetime import datetime
from urllib import request, parse, error

HUBSPOT_API = "https://api.hubapi.com"
TOKEN = os.environ.get("HUBSPOT_TOKEN", "")
SCORE_PROPERTY = os.environ.get("SCORE_PROPERTY", "auto_score_company_person_profile")
PORTAL_ID = os.environ.get("HUBSPOT_PORTAL_ID", "3073236")

LEADS_PATH = "public/data/leads.json"
STICKY_PATH = "public/data/sticky.json"

# Properties we fetch on the main snapshot.
PROPERTIES = [
    "createdate", "firstname", "lastname", "email",
    "lifecyclestage", "hs_lead_status", "hs_analytics_source",
    SCORE_PROPERTY,
    "auto_score_engagement_maturity",
    "auto_score_lead",
    # Contact-level scoring criteria — feed the in-browser scoring simulator.
    "hs_seniority",
    "job_department",
    "hs_email_optout",
]

# Company-level scoring criteria — fetched separately via /companies/batch/read
# (these are properties on the contact's associated Primary Company, NOT on the contact).
COMPANY_PROPERTIES = [
    "type_d_acteur",
    "type_dacteur__other",
]


def hubspot_request(path, method="GET", body=None, params=None):
    """Make a HubSpot API request with auth + basic 429 retry."""
    if not TOKEN:
        raise RuntimeError("HUBSPOT_TOKEN env var is empty.")

    url = HUBSPOT_API + path
    if params:
        url += "?" + parse.urlencode(params)

    headers = {
        "Authorization": "Bearer " + TOKEN,
        "Content-Type": "application/json",
    }
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = request.Request(url, data=data, method=method, headers=headers)

    for attempt in range(5):
        try:
            with request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except error.HTTPError as e:
            if e.code == 429:
                # Rate-limited — backoff
                wait = 2 ** attempt
                print("    [429] rate-limited, sleeping " + str(wait) + "s...")
                time.sleep(wait)
                continue
            raise
    raise RuntimeError("HubSpot API gave up after 5 attempts: " + url)


def fetch_all_contacts():
    """Paginate through every contact in the portal.

    We also request the `associations.companies` so each contact comes
    back with its associated company IDs — needed to look up `type_d_acteur`
    on the company side (those are company-level properties, not contact
    properties).
    """
    print("Fetching contacts (score property: " + SCORE_PROPERTY + ")...")
    contacts = []
    after = None
    while True:
        params = {
            "limit": "100",
            "properties": ",".join(PROPERTIES),
            "associations": "companies",
        }
        if after:
            params["after"] = after
        data = hubspot_request("/crm/v3/objects/contacts", params=params)
        contacts.extend(data.get("results", []))
        if len(contacts) % 1000 == 0 and len(contacts) > 0:
            print("  fetched " + str(len(contacts)) + " so far...")
        paging = data.get("paging", {}).get("next", {})
        after = paging.get("after")
        if not after:
            break
    print("Fetched " + str(len(contacts)) + " contacts.")
    return contacts


def primary_company_id(contact):
    """Find the primary company ID for a contact.

    HubSpot returns associations under `associations.companies.results` as a
    list of {id, type} objects. The PRIMARY association uses the type code
    `contact_to_company` (id 1 in the legacy v3 API) which appears as the
    first entry by default — but to be safe we look for it explicitly.
    Falls back to the first company in the list if no explicit "primary"
    type is found.
    """
    assocs = (contact.get("associations") or {}).get("companies") or {}
    results = assocs.get("results") or []
    if not results:
        return None
    # Look for the explicit primary association type. HubSpot uses several
    # type names depending on API version: "contact_to_company" (label-free
    # primary), or the typeId 1.
    for r in results:
        t = r.get("type") or ""
        if t == "contact_to_company" or r.get("typeId") == 1:
            return str(r.get("id"))
    # Otherwise return the first one as a best-effort fallback.
    return str(results[0].get("id"))


def batch_fetch_companies(company_ids, props):
    """Batch-read companies (max 100 per call) and return a dict
    {companyId: {prop1: value, prop2: value, ...}}."""
    out = {}
    company_ids = list({c for c in company_ids if c})  # de-dup, drop empties
    print("Fetching " + str(len(company_ids)) + " unique companies for "
          + ", ".join(props) + " ...")
    for i in range(0, len(company_ids), 100):
        chunk = company_ids[i:i+100]
        body = {
            "inputs": [{"id": cid} for cid in chunk],
            "properties": props,
        }
        try:
            data = hubspot_request("/crm/v3/objects/companies/batch/read",
                                   method="POST", body=body)
            for r in data.get("results", []):
                cid = str(r.get("id"))
                out[cid] = r.get("properties") or {}
        except Exception as e:
            print("  warn: company batch " + str(i) + "-" + str(i+len(chunk))
                  + " failed: " + str(e))
        if (i + 100) % 500 == 0:
            print("  companies fetched: " + str(min(i + 100, len(company_ids)))
                  + "/" + str(len(company_ids)))
    print("Done. " + str(len(out)) + " companies cached.")
    return out


def get_contact_history(contact_id, props):
    """Fetch property history for a single contact.

    Returns dict { property_name: [ {value, timestamp}, ... ] } ordered most-recent-first.
    """
    params = {
        "propertiesWithHistory": ",".join(props),
        "archived": "false",
    }
    data = hubspot_request("/crm/v3/objects/contacts/" + str(contact_id), params=params)
    history_raw = data.get("propertiesWithHistory") or {}
    out = {}
    for prop in props:
        entries = history_raw.get(prop) or []
        # HubSpot returns most-recent first; we keep that order
        out[prop] = [
            {"value": e.get("value"), "timestamp": e.get("timestamp")}
            for e in entries
        ]
    return out


def first_open_deal_conversion(history):
    """Given a contact's property history, find:
      - the EARLIEST timestamp where hs_lead_status became OPEN_DEAL
      - the lifecyclestage that was active at that moment

    Returns None if the contact has never been OPEN_DEAL.
    """
    status_history = history.get("hs_lead_status") or []
    lifecycle_history = history.get("lifecyclestage") or []

    # Find earliest entry with value=OPEN_DEAL. History is most-recent-first, so scan from end.
    open_deal_entries = [e for e in status_history if e.get("value") == "OPEN_DEAL"]
    if not open_deal_entries:
        return None
    # Earliest = the one with the smallest timestamp.
    earliest = min(open_deal_entries, key=lambda e: e.get("timestamp") or "")
    open_deal_at = earliest.get("timestamp")
    if not open_deal_at:
        return None

    # Now find the lifecycle stage that was active at open_deal_at.
    # We want the most recent lifecycle entry whose timestamp <= open_deal_at.
    # If none qualifies (the contact had no lifecycle set before that), use earliest lifecycle.
    lifecycle_at = None
    # Iterate from oldest to newest so we can track what was "current" at each point
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

    # Fallback: if no entry was before the open_deal moment, use the first one chronologically
    if not lifecycle_at and sorted_lc:
        lifecycle_at = sorted_lc[0].get("value")

    return {
        "firstOpenDealAt": open_deal_at,
        "lifecycleAtConversion": lifecycle_at or "",
    }


def lifecycle_passages(history):
    """Set of every distinct lifecyclestage the contact has ever been at."""
    lifecycle_history = history.get("lifecyclestage") or []
    stages = set()
    for entry in lifecycle_history:
        v = entry.get("value")
        if v:
            stages.add(v)
    return stages


def load_sticky():
    """Load existing sticky.json, return dict with normalized shape."""
    try:
        with open(STICKY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {
            "openDealIds": [], "conversions": {},
            "everLeadIds": [], "everMqlIds": [], "everSqlIds": [],
            "everSubscriberIds": [], "everOpportunityIds": [], "everCustomerIds": [],
            "companies": {},
        }

    # Normalize: keep all fields, default missing ones to empty.
    return {
        "openDealIds":        data.get("openDealIds", []),
        "conversions":        data.get("conversions", {}),
        "everLeadIds":        data.get("everLeadIds", []),
        "everMqlIds":         data.get("everMqlIds", []),
        "everSqlIds":         data.get("everSqlIds", []),
        "everSubscriberIds":  data.get("everSubscriberIds", []),
        "everOpportunityIds": data.get("everOpportunityIds", []),
        "everCustomerIds":    data.get("everCustomerIds", []),
        # Cache of company-level scoring data:
        # {companyId: {"type_d_acteur": "...", "type_dacteur__other": "..."}}
        "companies":          data.get("companies", {}),
    }


def write_sticky(sticky):
    with open(STICKY_PATH, "w", encoding="utf-8") as f:
        json.dump(sticky, f, ensure_ascii=False, indent=2)


def main():
    contacts = fetch_all_contacts()
    sticky = load_sticky()
    sticky_ids = set(str(x) for x in sticky["openDealIds"])
    conversions = dict(sticky["conversions"])  # copy

    # Load the lifecycle-history sets (empty if old sticky format).
    ever_lead_ids        = set(str(x) for x in sticky["everLeadIds"])
    ever_mql_ids         = set(str(x) for x in sticky["everMqlIds"])
    ever_sql_ids         = set(str(x) for x in sticky["everSqlIds"])
    ever_subscriber_ids  = set(str(x) for x in sticky["everSubscriberIds"])
    ever_opportunity_ids = set(str(x) for x in sticky["everOpportunityIds"])
    ever_customer_ids    = set(str(x) for x in sticky["everCustomerIds"])

    # Quick incremental check on current snapshot: contacts whose CURRENT
    # lifecycle is one of these stages are obviously "ever" in that stage.
    # Cheap win that catches new contacts without an extra API call.
    for c in contacts:
        cid = str(c.get("id"))
        cur = (c.get("properties") or {}).get("lifecyclestage") or ""
        if cur == "lead": ever_lead_ids.add(cid)
        elif cur == "marketingqualifiedlead": ever_mql_ids.add(cid)
        elif cur == "salesqualifiedlead": ever_sql_ids.add(cid)
        elif cur == "subscriber": ever_subscriber_ids.add(cid)
        elif cur == "opportunity": ever_opportunity_ids.add(cid)
        elif cur == "customer": ever_customer_ids.add(cid)

    # Detect new Open Deal contacts (current status = OPEN_DEAL) not yet in sticky
    new_open_deal_ids = []
    for c in contacts:
        cid = str(c.get("id"))
        props = c.get("properties") or {}
        if props.get("hs_lead_status") == "OPEN_DEAL" and cid not in sticky_ids:
            new_open_deal_ids.append(cid)
            sticky_ids.add(cid)

    print("New Open Deal contacts since last run: " + str(len(new_open_deal_ids)))

    # For each new one, fetch history to capture firstOpenDealAt + lifecycleAtConversion
    # AND any lifecycle stage passages we hadn't seen yet.
    for i, cid in enumerate(new_open_deal_ids):
        try:
            history = get_contact_history(cid, ["hs_lead_status", "lifecyclestage"])
            conv = first_open_deal_conversion(history)
            if conv:
                conversions[cid] = conv
            # Capture lifecycle passages from history too
            stages = lifecycle_passages(history)
            if "lead" in stages: ever_lead_ids.add(cid)
            if "marketingqualifiedlead" in stages: ever_mql_ids.add(cid)
            if "salesqualifiedlead" in stages: ever_sql_ids.add(cid)
            if "subscriber" in stages: ever_subscriber_ids.add(cid)
            if "opportunity" in stages: ever_opportunity_ids.add(cid)
            if "customer" in stages: ever_customer_ids.add(cid)
        except Exception as e:
            print("  warn: could not fetch history for " + cid + ": " + str(e))
        if (i + 1) % 25 == 0:
            print("  enriched " + str(i + 1) + "/" + str(len(new_open_deal_ids)))
        time.sleep(0.1)  # gentle rate limit

    print("Sticky Open Deal IDs: " + str(len(sticky_ids)) + " total ("
          + str(len(new_open_deal_ids)) + " new this run, "
          + str(len(sticky_ids) - len(new_open_deal_ids)) + " pre-existing).")
    print("Conversions tracked: " + str(len(conversions)))
    print("Ever Lead: " + str(len(ever_lead_ids)) + " | Ever MQL: " + str(len(ever_mql_ids))
          + " | Ever SQL: " + str(len(ever_sql_ids)))

    # ----- Company resolution for the simulator -----
    # type_d_acteur and type_dacteur__other are COMPANY-level properties, not
    # contact-level. We resolve each contact's primary company, dedupe, and
    # batch-fetch only the companies we don't already have cached.
    companies_cache = dict(sticky["companies"])  # copy: {companyId: {props}}

    # Map contact_id -> primary company_id
    contact_to_company = {}
    for c in contacts:
        cid = str(c.get("id"))
        company_id = primary_company_id(c)
        if company_id:
            contact_to_company[cid] = company_id

    print("Resolved primary company for " + str(len(contact_to_company))
          + "/" + str(len(contacts)) + " contacts.")

    # Find which company IDs we still need to fetch (not in cache)
    needed_company_ids = {
        cid for cid in contact_to_company.values() if cid not in companies_cache
    }
    if needed_company_ids:
        new_company_props = batch_fetch_companies(list(needed_company_ids), COMPANY_PROPERTIES)
        companies_cache.update(new_company_props)
    else:
        print("All companies already in cache — no extra API call needed.")

    # Now build leads.json
    leads = []
    for c in contacts:
        cid = str(c.get("id"))
        props = c.get("properties") or {}

        def to_num(v):
            try:
                return float(v) if v not in (None, "") else 0.0
            except (TypeError, ValueError):
                return 0.0

        # Look up the contact's primary company data for company-level criteria
        company_id = contact_to_company.get(cid)
        company_props = companies_cache.get(company_id, {}) if company_id else {}

        conv = conversions.get(cid)
        lead = {
            "id": cid,
            "firstname": props.get("firstname") or "",
            "lastname": props.get("lastname") or "",
            "email": props.get("email") or "",
            "created": props.get("createdate") or "",
            "lifecyclestage": props.get("lifecyclestage") or "",
            "status": props.get("hs_lead_status") or "",
            "source": props.get("hs_analytics_source") or "",
            "score": to_num(props.get(SCORE_PROPERTY)),
            "scoreEngagement": to_num(props.get("auto_score_engagement_maturity")),
            "scoreLead": to_num(props.get("auto_score_lead")),
            "everOpenDeal": cid in sticky_ids,
            "everLead": cid in ever_lead_ids,
            "everMql": cid in ever_mql_ids,
            "everSql": cid in ever_sql_ids,
            "everOpportunity": cid in ever_opportunity_ids,
            "everCustomer": cid in ever_customer_ids,
            # Raw scoring-criteria values from HubSpot — fed to the in-browser
            # scoring simulator. Contact-level props come from the contact itself,
            # company-level props come from the primary company association.
            "criteria": {
                "hs_seniority":         props.get("hs_seniority") or "",
                "job_department":       props.get("job_department") or "",
                "type_d_acteur":        company_props.get("type_d_acteur") or "",
                "type_dacteur__other":  company_props.get("type_dacteur__other") or "",
                "hs_email_optout":      props.get("hs_email_optout") or "",
            },
        }
        if conv:
            lead["firstOpenDealAt"] = conv.get("firstOpenDealAt")
            lead["lifecycleAtConversion"] = conv.get("lifecycleAtConversion")
        leads.append(lead)

    # Save sticky AT THE END so the companies cache (just populated above)
    # is persisted for future runs.
    sticky_out = {
        "openDealIds":        sorted(sticky_ids),
        "conversions":        conversions,
        "everLeadIds":        sorted(ever_lead_ids),
        "everMqlIds":         sorted(ever_mql_ids),
        "everSqlIds":         sorted(ever_sql_ids),
        "everSubscriberIds":  sorted(ever_subscriber_ids),
        "everOpportunityIds": sorted(ever_opportunity_ids),
        "everCustomerIds":    sorted(ever_customer_ids),
        "companies":          companies_cache,
    }
    write_sticky(sticky_out)
    print("Companies cached: " + str(len(companies_cache)))

    out = {
        "fetchedAt": datetime.utcnow().isoformat() + "Z",
        "total": len(leads),
        "scoreProperty": SCORE_PROPERTY,
        "portalId": PORTAL_ID,
        "stickyOpenDealCount": len(sticky_ids),
        "leads": leads,
    }
    with open(LEADS_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    size = os.path.getsize(LEADS_PATH)
    print("Wrote " + LEADS_PATH + " (" + format(size, ",") + " bytes).")


if __name__ == "__main__":
    main()
