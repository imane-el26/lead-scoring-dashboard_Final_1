"""
Sonde les endpoints HubSpot nécessaires pour récupérer les variables détaillées
du score "Engagement & Maturity" (page visits par URL, form submissions,
workflow enrollment, segment membership) — SANS rien écrire, juste pour savoir
ce qui est accessible avec votre token/tier avant de construire le pipeline complet.

Usage :
  export HUBSPOT_TOKEN="pat-na1-..."
  python3 scripts/probe_hubspot_apis.py [contact_id]

Si aucun contact_id n'est donné, on prend le premier contact trouvé dans
public/data/leads-0.json (ou leads.json).
"""

import json
import os
import sys
from urllib import request, parse, error

HUBSPOT_API = "https://api.hubapi.com"
TOKEN = os.environ.get("HUBSPOT_TOKEN", "")


def hubspot_get(path, params=None):
    """Fait un GET et renvoie (status_code, body_dict_or_text) sans lever d'exception."""
    url = HUBSPOT_API + path
    if params:
        url += "?" + parse.urlencode(params)
    headers = {"Authorization": "Bearer " + TOKEN}
    req = request.Request(url, method="GET", headers=headers)
    try:
        with request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            try:
                return resp.status, json.loads(body)
            except json.JSONDecodeError:
                return resp.status, body
    except error.HTTPError as e:
        body = e.read().decode("utf-8")
        try:
            return e.code, json.loads(body)
        except json.JSONDecodeError:
            return e.code, body


def pick_contact_id():
    for fname in ("public/data/leads-0.json", "public/data/leads.json"):
        if os.path.exists(fname):
            with open(fname, "r", encoding="utf-8") as f:
                data = json.load(f)
            leads = data.get("leads") or []
            if leads:
                return leads[0]["id"]
    raise RuntimeError("Aucun contact_id fourni et aucun fichier leads trouvé. "
                        "Passez un ID en argument: python3 scripts/probe_hubspot_apis.py 12345")


def summarize(label, status, body):
    ok = "OK " if status < 300 else ("BLOQUE" if status in (401, 403) else "ERREUR")
    print(f"\n[{ok}] {label}  (HTTP {status})")
    snippet = json.dumps(body, ensure_ascii=False)[:500] if isinstance(body, (dict, list)) else str(body)[:500]
    print("  " + snippet)


def main():
    if not TOKEN:
        print("Erreur: HUBSPOT_TOKEN n'est pas défini.")
        sys.exit(1)

    contact_id = sys.argv[1] if len(sys.argv) > 1 else pick_contact_id()
    print(f"Sondage des APIs HubSpot pour le contact {contact_id}...\n")
    print("=" * 70)

    # 1. Forms API — liste des formulaires du portail (pour repérer l'ID du
    #    formulaire "Main" et ensuite en tirer les soumissions précises).
    status, body = hubspot_get("/marketing/v3/forms", params={"limit": "20"})
    summarize("Forms API — liste des formulaires (repérer l'ID de 'Main')", status, body)

    # 2. Engagements API legacy — meetings associés au contact (type=MEETING
    #    dans la réponse). Ancienne API, souvent accessible sans scope dédié
    #    au-delà de la lecture des contacts.
    status, body = hubspot_get(
        f"/engagements/v1/engagements/associated/CONTACT/{contact_id}/paged",
        params={"limit": "10"},
    )
    summarize("Engagements API (legacy) — meetings/activités associées au contact", status, body)

    # 4. Contact property hs_calculated_form_submissions (alternative sans Enterprise)
    status, body = hubspot_get(
        f"/crm/v3/objects/contacts/{contact_id}",
        params={"properties": "hs_calculated_form_submissions"},
    )
    summarize("Propriété hs_calculated_form_submissions (fallback formulaires)", status, body)

    # 5. Segment/list membership (par contact) — 0-1 = objectTypeId des contacts
    status, body = hubspot_get(f"/crm/v3/lists/records/0-1/{contact_id}/memberships")
    summarize("Lists API — segments dont le contact est membre", status, body)

    # 6. Recherche du nom interne exact des 2 propriétés posées par les
    #    workflows "Contacts – Origine AI Referrals" et "Lead Scoring |
    #    Inbound Message" — pas besoin des scopes automation/Workflows API,
    #    ce sont de simples propriétés de contact à lire directement.
    status, body = hubspot_get("/crm/v3/properties/contacts")
    if status < 300 and isinstance(body, dict):
        keywords = ("referral", "message", "ai_referral", "sent_a_message", "inbound",
                    "latest_source", "latest_traffic", "source_data", "marketing_contact")
        matches = [
            {"name": p.get("name"), "label": p.get("label")}
            for p in body.get("results", [])
            if any(k in (p.get("name") or "").lower() or k in (p.get("label") or "").lower()
                   for k in keywords)
        ]
        summarize("Propriétés contact — candidats pour 'AI Referrals' / 'Has Sent a Message'",
                   status, matches)
    else:
        summarize("Propriétés contact — liste complète (recherche manuelle nécessaire)", status, body)

    # 7. Vérification directe des noms de propriétés contact/company ajoutés
    #    pour l'onglet Data quality — on vérifie que le nom EXACT existe (pas
    #    juste un mot-clé approchant), pour repérer tout de suite une faute
    #    de frappe avant de lancer fetch_leads.py sur tout le portail.
    contact_props_to_check = [
        "reasoning_contact", "lemlistlinkedinurl", "meeting_type_cp__c",
        "hs_analytics_last_url", "message", "hs_google_click_id",
        "last_webinar_seen", "origine_event",
    ]
    if status < 300 and isinstance(body, dict):
        known_names = {p.get("name") for p in body.get("results", [])}
        found = {name: (name in known_names) for name in contact_props_to_check}
        summarize("Propriétés contact — présence exacte des noms Data quality", status, found)
    else:
        summarize("Propriétés contact — liste complète indisponible, vérification manuelle nécessaire",
                   status, body)

    status, body = hubspot_get("/crm/v3/properties/companies")
    company_props_to_check = ["type_d_acteur", "type_dacteur__other",
                               "linkedin_company_page", "reasoning_company"]
    if status < 300 and isinstance(body, dict):
        known_names = {p.get("name") for p in body.get("results", [])}
        found = {name: (name in known_names) for name in company_props_to_check}
        summarize("Propriétés company — présence exacte des noms Data quality", status, found)
    else:
        summarize("Propriétés company — liste complète indisponible, vérification manuelle nécessaire",
                   status, body)

    # 8. Vérifie que l'association "Primary" contact <-> deal existe bien
    #    dans ce portail avec typeId=1 (utilisé par batch_fetch_primary_contact_flags
    #    dans fetch_leads.py pour déterminer le "main contact" d'un Open Deal).
    status, body = hubspot_get("/crm/v4/associations/contacts/deals/labels")
    summarize("Association labels contact -> deal (cherche typeId=1, label='Primary')",
               status, body)

    print("\n" + "=" * 70)
    print("Lecture : [OK] = exploitable tel quel. [BLOQUE] = 401/403, probablement")
    print("un problème de scope du token ou de tier HubSpot. [ERREUR] = autre souci")
    print("(ex: contact_id invalide, endpoint mal formé) — regardez le message HubSpot.")


if __name__ == "__main__":
    main()
