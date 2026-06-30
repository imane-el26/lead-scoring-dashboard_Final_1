"""
Generate fake leads + a sticky.json with conversions data, for local UI testing
without hitting the HubSpot API.

Run with:
  python3 scripts/test_local.py

Then serve the dashboard locally with:
  python3 -m http.server 8000 --directory public
And open http://localhost:8000
"""

import json
import os
import random
from datetime import datetime, timedelta

random.seed(42)

LEADS_PATH = "public/data/leads.json"
STICKY_PATH = "public/data/sticky.json"

SOURCES = [
    "DIRECT_TRAFFIC", "ORGANIC_SEARCH", "PAID_SEARCH",
    "REFERRALS", "SOCIAL_MEDIA", "EMAIL_MARKETING", "OTHER_CAMPAIGNS",
]
LIFECYCLES = [
    "subscriber", "lead", "marketingqualifiedlead",
    "salesqualifiedlead", "opportunity", "customer",
]
STATUSES = ["NEW", "OPEN", "IN_PROGRESS", "OPEN_DEAL", "CONNECTED", "UNQUALIFIED"]

N = 500
now = datetime.utcnow()

# Generate contacts
leads = []
sticky_ids = []
conversions = {}

for i in range(N):
    cid = str(100000 + i)
    days_ago = random.randint(0, 1000)  # up to ~3 years old, for richer history
    created = now - timedelta(days=days_ago, hours=random.randint(0, 23))
    score = random.choice([
        random.randint(-10, 0),
        random.randint(1, 20),
        random.randint(21, 40),
        random.randint(41, 60),
        random.randint(61, 80),
        random.randint(81, 100),
    ])
    status = random.choices(
        STATUSES,
        weights=[15, 20, 20, 8, 25, 12],  # OPEN_DEAL is now rare in current state
        k=1,
    )[0]
    lifecycle = random.choice(LIFECYCLES)

    lead = {
        "id": cid,
        "firstname": "Imane" if i % 5 == 0 else "Test",
        "lastname": "User " + str(i),
        "email": "test" + str(i) + "@example.com",
        "created": created.isoformat() + "Z",
        "lifecyclestage": lifecycle,
        "status": status,
        "source": random.choice(SOURCES),
        "score": float(score),
        "scoreEngagement": float(random.randint(0, 100)),
        "scoreLead": float(random.randint(0, 100)),
        "everOpenDeal": False,
    }

    # ~40% of contacts have a historical Open Deal conversion at some point
    if random.random() < 0.40:
        lead["everOpenDeal"] = True
        sticky_ids.append(cid)
        # The conversion happened somewhere between created and now
        max_offset = (now - created).total_seconds()
        offset = random.uniform(0, max_offset)
        conv_at = (created + timedelta(seconds=offset)).isoformat() + "Z"
        # The lifecycle at conversion is one of these — weighted toward MQL/SQL/lead
        lc_at = random.choices(
            LIFECYCLES,
            weights=[5, 30, 35, 20, 8, 2],
            k=1,
        )[0]
        conversions[cid] = {
            "firstOpenDealAt": conv_at,
            "lifecycleAtConversion": lc_at,
        }
        lead["firstOpenDealAt"] = conv_at
        lead["lifecycleAtConversion"] = lc_at

    leads.append(lead)

# Write leads.json
leads_out = {
    "fetchedAt": now.isoformat() + "Z",
    "total": len(leads),
    "scoreProperty": "auto_score_company_person_profile",
    "portalId": "3073236",
    "stickyOpenDealCount": len(sticky_ids),
    "leads": leads,
}
os.makedirs("public/data", exist_ok=True)
with open(LEADS_PATH, "w", encoding="utf-8") as f:
    json.dump(leads_out, f, ensure_ascii=False)
print("Generated " + str(N) + " fake leads (" + str(len(sticky_ids)) + " sticky Open Deal).")

# Write sticky.json
sticky_out = {
    "openDealIds": sorted(sticky_ids),
    "conversions": conversions,
}
with open(STICKY_PATH, "w", encoding="utf-8") as f:
    json.dump(sticky_out, f, ensure_ascii=False, indent=2)
print("Generated sticky.json with " + str(len(sticky_ids)) + " ids and "
      + str(len(conversions)) + " conversion records.")
print()
print("Now serve with:  python3 -m http.server 8000 --directory public")
print("Then open:       http://localhost:8000")
