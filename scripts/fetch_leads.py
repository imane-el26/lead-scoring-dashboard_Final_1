"""
Fetch contacts from HubSpot and write public/data/leads.json + sticky.json
+ public/data/companies.json.

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
  5. For each NEW Open Deal conversion, also captures what each
     INTERACTION_HISTORY_PROPERTIES signal's value was at that exact moment
     (`interactionAtConversion`) — see backfill_interaction_history.py to
     backfill this for conversions detected before this existed.
  6. Resolves the "main contact" deal association for every ever-Open-Deal
     contact (`isMainContact`).
  7. Fetches EVERY company in the portal (not just ones linked to a contact)
     and writes public/data/companies.json — the true "total company base"
     for the Data quality tab's Companies panel.

For the initial backfill (when sticky.json has very few historical entries), run
`backfill_history.py` manually first, then `backfill_interaction_history.py`.
After that, this daily script handles new conversions incrementally.
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
    # Whether this contact counts against the portal's Marketing Contacts
    # tier — feeds the Data quality tab's "Marketing contact" filter.
    "hs_marketable_status",
    SCORE_PROPERTY,
    "auto_score_engagement_maturity",
    "auto_score_lead",
    # Contact-level scoring criteria — feed the in-browser scoring simulator.
    "hs_seniority",
    "job_department",
    "hs_email_optout",
    # Engagement/behavioral signals — raw inputs for the Engagement & Maturity
    # ML analysis (see notebooks/). These are native HubSpot analytics
    # counters, not something we compute ourselves.
    "hs_analytics_num_page_views",
    "hs_analytics_num_visits",
    "hs_email_open",
    "hs_email_click",
    "hs_email_bounce",
    "hs_email_delivered",
    "num_conversion_events",
    "recent_conversion_event_name",
    "hs_latest_meeting_activity",
    "hs_predictivecontactscore_v2",
    # First-touch attribution — proxy for "Ads events" (only captures the
    # contact's ORIGINAL touch, not every subsequent ad interaction).
    "hs_analytics_source_data_1",
    "hs_analytics_source_data_2",
    # Set directly by the 2 workflows behind "Workflows events" in the
    # Engagement & Maturity grid — read as plain contact properties instead
    # of querying the Workflows API (no automation scope needed).
    "enrolled_ai_referrals",
    # Display label is "Has Sent a Message ?" — HubSpot internal property
    # names can't contain "?", so this is the sanitized form. Verify against
    # scripts/probe_hubspot_apis.py (step 6 — properties search) if it 404s.
    "has_sent_a_message",
    # Data quality tab — current-value snapshot of these properties (see
    # CONTACT_QUALITY_PROPERTIES below for the full list; only the ones not
    # already covered above need adding here).
    "reasoning_contact",
    "lemlistlinkedinurl",
    "meeting_type_cp__c",
    "hs_analytics_last_url",
    "message",
    "hs_google_click_id",
    "last_webinar_seen",
    "origine_event",
]

# Company-level properties — fetched via a full paginated listing of every
# company in the portal (see fetch_all_companies()), not just companies
# linked to a contact.
COMPANY_PROPERTIES = [
    "name",
    "createdate",
    "type_d_acteur",
    "type_dacteur__other",
    "linkedin_company_page",
    "reasoning_company",
]

# The exact set of contact-level properties surfaced in the Data quality tab
# (lead.quality). Kept as one list so PROPERTIES above and the "quality" dict
# built in main() can't silently drift out of sync.
CONTACT_QUALITY_PROPERTIES = [
    "reasoning_contact",
    "lemlistlinkedinurl",
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

COMPANY_QUALITY_PROPERTIES = [
    "type_d_acteur",
    "type_dacteur__other",
    "linkedin_company_page",
    "reasoning_company",
]

# Subset of CONTACT_QUALITY_PROPERTIES we track full HubSpot change history
# for, so we can answer "what was this contact's interaction/engagement like
# at the exact moment they converted to Open Deal?" (see
# first_open_deal_conversion() / value_at_timestamp() below). Excludes
# reasoning_contact and lemlistlinkedinurl — those are enrichment fields, not
# interaction signals, so only their current value matters.
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


def fetch_all_companies():
    """Paginate through EVERY company in the portal — not just ones linked
    to a contact we happen to have. Needed for the Data quality tab's true
    "total company base" (a company with zero associated contacts is
    invisible if we only ever look at it via contact associations).
    """
    print("Fetching all companies (properties: " + ", ".join(COMPANY_PROPERTIES) + ")...")
    companies = []
    after = None
    while True:
        params = {
            "limit": "100",
            "properties": ",".join(COMPANY_PROPERTIES),
        }
        if after:
            params["after"] = after
        data = hubspot_request("/crm/v3/objects/companies", params=params)
        companies.extend(data.get("results", []))
        if len(companies) % 2000 == 0 and len(companies) > 0:
            print("  fetched " + str(len(companies)) + " companies so far...")
        paging = data.get("paging", {}).get("next", {})
        after = paging.get("after")
        if not after:
            break
    print("Fetched " + str(len(companies)) + " companies.")
    return companies


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


def value_at_timestamp(entries, target_ts):
    """Given a property's history entries [{value, timestamp}, ...] (any
    order), find the value that was ACTIVE at target_ts — i.e. the most
    recent entry with timestamp <= target_ts. Falls back to the earliest
    known entry if every entry is AFTER target_ts (the property was set for
    the first time after that moment). Returns None if there's no history
    at all for this property.

    Used to answer "what was this contact's engagement signal at the exact
    moment they converted to Open Deal?" — same idea as the
    lifecycleAtConversion logic in first_open_deal_conversion().
    """
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


def batch_fetch_main_contact_flags(contact_ids):
    """For each contact ID, determine whether they're the "main contact" on
    any of their associated deals, via the v4 Associations API.

    This portal uses a CUSTOM association label for this — not HubSpot's
    generic "Primary" (verified via scripts/probe_hubspot_apis.py, step 8):
    {"category": "USER_DEFINED", "typeId": 13, "label": "main contact"}.
    We match on typeId+category (stable per-portal identifiers) with the
    label text as a fallback, in case the typeId ever differs between
    environments (e.g. sandbox vs. production).

    Returns {contactId: True/False/None}. None means the contact currently
    has no deal association at all (nothing to be "main contact" on) — e.g.
    their deal was deleted, or reassigned since they converted.
    """
    out = {}
    contact_ids = list({str(c) for c in contact_ids if c})
    print("Fetching deal associations for " + str(len(contact_ids)) + " Open Deal contacts...")
    for i in range(0, len(contact_ids), 1000):  # v4 associations batch cap
        chunk = contact_ids[i:i + 1000]
        body = {"inputs": [{"id": cid} for cid in chunk]}
        try:
            data = hubspot_request(
                "/crm/v4/associations/contacts/deals/batch/read",
                method="POST", body=body,
            )
            for r in data.get("results", []):
                cid = str((r.get("from") or {}).get("id"))
                deals = r.get("to") or []
                if not deals:
                    out[cid] = None
                    continue
                out[cid] = any(
                    (at.get("category") == "USER_DEFINED" and at.get("typeId") == 13)
                    or (at.get("label") or "").strip().lower() == "main contact"
                    for d in deals
                    for at in (d.get("associationTypes") or [])
                )
        except Exception as e:
            print("  warn: primary-contact batch " + str(i) + "-" + str(i + len(chunk))
                  + " failed: " + str(e))
    print("Done. " + str(len(out)) + " contacts checked for main-contact-on-deal.")
    return out


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
        }

    # Normalize: keep all fields, default missing ones to empty.
    # Note: "companies" used to be cached here, but companies are now always
    # fully re-fetched every run (see fetch_all_companies()) — a stale-cache
    # class of bug otherwise, since new COMPANY_PROPERTIES additions would
    # silently never reach companies fetched before they existed. Any old
    # sticky.json with a "companies" key just has it dropped on next write.
    return {
        "openDealIds":        data.get("openDealIds", []),
        "conversions":        data.get("conversions", {}),
        "everLeadIds":        data.get("everLeadIds", []),
        "everMqlIds":         data.get("everMqlIds", []),
        "everSqlIds":         data.get("everSqlIds", []),
        "everSubscriberIds":  data.get("everSubscriberIds", []),
        "everOpportunityIds": data.get("everOpportunityIds", []),
        "everCustomerIds":    data.get("everCustomerIds", []),
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
    # AND any lifecycle stage passages we hadn't seen yet. We also pull the
    # INTERACTION_HISTORY_PROPERTIES history in the SAME call (propertiesWithHistory
    # is comma-separated, so this costs zero extra API requests) so we can compute
    # what each interaction signal's value was at the exact moment of conversion.
    for i, cid in enumerate(new_open_deal_ids):
        try:
            history = get_contact_history(
                cid, ["hs_lead_status", "lifecyclestage"] + INTERACTION_HISTORY_PROPERTIES
            )
            conv = first_open_deal_conversion(history)
            if conv:
                conv["interactionAtConversion"] = {
                    prop: value_at_timestamp(history.get(prop) or [], conv["firstOpenDealAt"])
                    for prop in INTERACTION_HISTORY_PROPERTIES
                }
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

    # ----- Main-contact-on-deal flag -----
    # Only meaningful for contacts who have ever been Open Deal — everyone
    # else gets None (see batch_fetch_main_contact_flags docstring).
    main_contact_flags = batch_fetch_main_contact_flags(sticky_ids)

    # ----- Company resolution -----
    # type_d_acteur / type_dacteur__other / linkedin_company_page /
    # reasoning_company are COMPANY-level properties, not contact-level.
    # We fetch EVERY company in the portal (not just ones linked to a
    # contact) so the Data quality tab's company totals are the real
    # portal-wide count, and to sidestep an entire class of "stale partial
    # cache" bugs from the old incremental-fetch approach.
    all_companies = fetch_all_companies()
    companies_cache = {
        str(c.get("id")): (c.get("properties") or {}) for c in all_companies
    }

    # Map contact_id -> primary company_id, for joining company props onto
    # each contact's criteria/quality below.
    contact_to_company = {}
    for c in contacts:
        cid = str(c.get("id"))
        company_id = primary_company_id(c)
        if company_id:
            contact_to_company[cid] = company_id

    print("Resolved primary company for " + str(len(contact_to_company))
          + "/" + str(len(contacts)) + " contacts.")

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
            # HubSpot's hs_marketable_status is a boolean property returned
            # as the string "true"/"false" — feeds the Data quality tab's
            # "Marketing contact" filter.
            "isMarketingContact": (props.get("hs_marketable_status") or "").lower() == "true",
            "score": to_num(props.get(SCORE_PROPERTY)),
            "scoreEngagement": to_num(props.get("auto_score_engagement_maturity")),
            "scoreLead": to_num(props.get("auto_score_lead")),
            "everOpenDeal": cid in sticky_ids,
            "everLead": cid in ever_lead_ids,
            "everMql": cid in ever_mql_ids,
            "everSql": cid in ever_sql_ids,
            "everOpportunity": cid in ever_opportunity_ids,
            "everCustomer": cid in ever_customer_ids,
            # Primary company association — lets the Data quality tab dedupe
            # contacts down to unique companies. No extra API cost: company_id
            # is already resolved above for the criteria/quality lookups.
            "companyId": company_id or "",
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
            # Raw engagement/behavioral signals — inputs for the Engagement &
            # Maturity ML analysis. Numeric counters default to 0 (HubSpot
            # leaves them blank when the contact has no activity yet).
            "engagement": {
                "numPageViews":          to_num(props.get("hs_analytics_num_page_views")),
                "numVisits":             to_num(props.get("hs_analytics_num_visits")),
                "emailOpens":            to_num(props.get("hs_email_open")),
                "emailClicks":           to_num(props.get("hs_email_click")),
                "emailBounces":          to_num(props.get("hs_email_bounce")),
                "emailDelivered":        to_num(props.get("hs_email_delivered")),
                "numConversionEvents":   to_num(props.get("num_conversion_events")),
                "recentConversionEvent": props.get("recent_conversion_event_name") or "",
                "latestMeetingActivity": props.get("hs_latest_meeting_activity") or "",
                "predictiveScoreV2":     to_num(props.get("hs_predictivecontactscore_v2")),
                "enrolledAiReferrals":   props.get("enrolled_ai_referrals") or "",
                "hasSentMessage":        props.get("has_sent_a_message") or "",
                "sourceDrilldown1":      props.get("hs_analytics_source_data_1") or "",
                "sourceDrilldown2":      props.get("hs_analytics_source_data_2") or "",
            },
            # Current-value snapshot of the Data quality tab's tracked
            # properties (contact-level + the primary company's). Keyed by
            # literal HubSpot property name — see CONTACT_QUALITY_PROPERTIES /
            # COMPANY_QUALITY_PROPERTIES above.
            "quality": {
                **{p: props.get(p) or "" for p in CONTACT_QUALITY_PROPERTIES},
                **{p: company_props.get(p) or "" for p in COMPANY_QUALITY_PROPERTIES},
            },
            # True/False if this contact has ever been Open Deal and we could
            # resolve their deal association(s); None if not applicable (never
            # Open Deal) or no deal is currently associated.
            "isMainContact": main_contact_flags.get(cid),
        }
        if conv:
            lead["firstOpenDealAt"] = conv.get("firstOpenDealAt")
            lead["lifecycleAtConversion"] = conv.get("lifecycleAtConversion")
            # Value of each INTERACTION_HISTORY_PROPERTIES signal at the exact
            # moment this contact first became Open Deal — only present for
            # conversions captured after this feature shipped (see
            # scripts/backfill_interaction_history.py for backfilling older ones).
            if "interactionAtConversion" in conv:
                lead["interactionAtConversion"] = conv["interactionAtConversion"]
        leads.append(lead)

    sticky_out = {
        "openDealIds":        sorted(sticky_ids),
        "conversions":        conversions,
        "everLeadIds":        sorted(ever_lead_ids),
        "everMqlIds":         sorted(ever_mql_ids),
        "everSqlIds":         sorted(ever_sql_ids),
        "everSubscriberIds":  sorted(ever_subscriber_ids),
        "everOpportunityIds": sorted(ever_opportunity_ids),
        "everCustomerIds":    sorted(ever_customer_ids),
    }
    write_sticky(sticky_out)

    # Meta info shared by every chunk
    meta = {
        "fetchedAt": datetime.utcnow().isoformat() + "Z",
        "total": len(leads),
        "scoreProperty": SCORE_PROPERTY,
        "portalId": PORTAL_ID,
        "stickyOpenDealCount": len(sticky_ids),
    }

    # Companies dataset — the Data quality tab's Companies panel reads this
    # directly instead of deriving companies from contacts (most companies
    # have zero associated contacts, so they'd otherwise never show up).
    companies_out = {
        "fetchedAt": meta["fetchedAt"],
        "total": len(all_companies),
        "companies": [
            {
                "id": str(c.get("id")),
                "name": (c.get("properties") or {}).get("name") or "",
                "created": (c.get("properties") or {}).get("createdate") or "",
                "type_d_acteur": (c.get("properties") or {}).get("type_d_acteur") or "",
                "type_dacteur__other": (c.get("properties") or {}).get("type_dacteur__other") or "",
                "linkedin_company_page": (c.get("properties") or {}).get("linkedin_company_page") or "",
                "reasoning_company": (c.get("properties") or {}).get("reasoning_company") or "",
            }
            for c in all_companies
        ],
    }
    with open("public/data/companies.json", "w", encoding="utf-8") as f:
        json.dump(companies_out, f, ensure_ascii=False)
    print("Wrote public/data/companies.json (" + str(len(all_companies)) + " companies).")

    # Cloudflare Workers/Pages caps assets at 25 MiB. Split into ~20 MB chunks
    # to stay safely under the limit. The dashboard fetches `leads-index.json`
    # first, then loads each chunk in parallel.
    MAX_BYTES_PER_CHUNK = 20 * 1024 * 1024   # 20 MB safe margin
    LEADS_DIR = os.path.dirname(LEADS_PATH)

    def write_chunk(chunk_leads, idx):
        chunk_path = os.path.join(LEADS_DIR, "leads-" + str(idx) + ".json")
        payload = dict(meta)
        payload["chunk"] = idx
        payload["leads"] = chunk_leads
        with open(chunk_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        return chunk_path, os.path.getsize(chunk_path)

    # First, write everything to a single file to measure total size
    with open(LEADS_PATH, "w", encoding="utf-8") as f:
        json.dump({**meta, "leads": leads}, f, ensure_ascii=False)
    total_size = os.path.getsize(LEADS_PATH)
    print("Single-file size: " + format(total_size, ",") + " bytes ("
          + str(round(total_size / 1024 / 1024, 1)) + " MB)")

    # Clean up any leftover chunk files from previous runs
    import glob
    for old_chunk in glob.glob(os.path.join(LEADS_DIR, "leads-*.json")):
        os.remove(old_chunk)

    if total_size <= MAX_BYTES_PER_CHUNK:
        # Small enough — keep as single file, write a 1-chunk index for
        # consistency.
        index = {**meta, "chunks": ["leads.json"], "totalChunks": 1}
        with open(os.path.join(LEADS_DIR, "leads-index.json"), "w", encoding="utf-8") as f:
            json.dump(index, f, ensure_ascii=False)
        print("Wrote " + LEADS_PATH + " (single file, no split needed).")
    else:
        # Need to split. Estimate avg lead size and pick a chunk size.
        avg_lead_bytes = total_size / max(1, len(leads))
        leads_per_chunk = max(1, int(MAX_BYTES_PER_CHUNK / avg_lead_bytes))
        print("Splitting " + str(len(leads)) + " leads into chunks of "
              + str(leads_per_chunk) + " (~"
              + str(round(leads_per_chunk * avg_lead_bytes / 1024 / 1024, 1))
              + " MB each)...")

        # Delete the single-file output since we are splitting
        os.remove(LEADS_PATH)

        chunk_filenames = []
        for i in range(0, len(leads), leads_per_chunk):
            chunk_idx = len(chunk_filenames)
            chunk_leads = leads[i:i + leads_per_chunk]
            chunk_path, chunk_size = write_chunk(chunk_leads, chunk_idx)
            chunk_filenames.append("leads-" + str(chunk_idx) + ".json")
            print("  wrote " + chunk_path + " (" + format(chunk_size, ",")
                  + " bytes, " + str(len(chunk_leads)) + " leads)")

        # Write the index file the dashboard reads first
        index = {
            **meta,
            "chunks": chunk_filenames,
            "totalChunks": len(chunk_filenames),
        }
        with open(os.path.join(LEADS_DIR, "leads-index.json"), "w", encoding="utf-8") as f:
            json.dump(index, f, ensure_ascii=False)
        print("Wrote " + str(len(chunk_filenames)) + " chunks + leads-index.json")


if __name__ == "__main__":
    main()
