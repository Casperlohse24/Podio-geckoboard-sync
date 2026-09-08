#!/usr/bin/env python3
"""
Podio -> Geckoboard sync.

Henter items fra en Podio-app og pusher dem ind i et Geckoboard dataset,
så et Geckoboard-widget kan vise data live (opdateres hver gang scriptet køres).

Kør scriptet med jævne mellemrum (fx via GitHub Actions cron, se
.github/workflows/sync.yml) for at holde dashboardet opdateret.

Alt konfiguration kommer fra miljøvariabler (se README.md for hvordan du
sætter dem op som GitHub Secrets).
"""

import os
import sys
import requests

# ---------------------------------------------------------------------------
# Konfiguration (fra miljøvariabler / GitHub Secrets)
# ---------------------------------------------------------------------------

PODIO_CLIENT_ID = os.environ["PODIO_CLIENT_ID"]
PODIO_CLIENT_SECRET = os.environ["PODIO_CLIENT_SECRET"]
PODIO_APP_ID = os.environ["PODIO_APP_ID"]
PODIO_APP_TOKEN = os.environ["PODIO_APP_TOKEN"]

GECKOBOARD_API_KEY = os.environ["GECKOBOARD_API_KEY"]
GECKOBOARD_DATASET_NAME = os.environ.get("GECKOBOARD_DATASET_NAME", "podio.items")
# Geckoboard kræver en valutakode (ISO 4217, fx "DKK", "EUR", "USD") for
# felter af typen "money".
GECKOBOARD_CURRENCY_CODE = os.environ.get("GECKOBOARD_CURRENCY_CODE", "DKK")

PODIO_API_BASE = "https://api.podio.com"
GECKOBOARD_API_BASE = "https://api.geckoboard.com"

# Hvor mange items der maks hentes pr. sync (Geckoboard datasets har et loft
# på 5.000 rækker, så juster om nødvendigt).
ITEM_LIMIT = int(os.environ.get("PODIO_ITEM_LIMIT", "200"))


# ---------------------------------------------------------------------------
# FELT-MAPPING — DENNE DEL SKAL DU TILPASSE TIL DIN PODIO-APP
# ---------------------------------------------------------------------------
# Definér her hvilke Podio-felter (identificeret ved deres "external_id",
# som du finder i Podio under App > Udvikler-info / Field settings) der skal
# ende som kolonner i Geckoboard, og hvilken Geckoboard-datatype de har.
#
# Geckoboard-typer: "string", "number", "date", "datetime", "money",
# "percentage", "boolean". Bemærk: "money"-felter kræver desuden en
# valutakode, se GECKOBOARD_CURRENCY_CODE ovenfor.
#
# Mapping herunder er til Podio-appen "Projects" (App ID 3276523):
#   - "title"          (Podio's indbyggede item-titel)
#   - "status"         (category felt, external_id "status")
#   - "go_live_date"   (date felt, external_id "go-live-1st-app")
#
# Skal du tilføje flere kolonner (fx money-felter som
# "monthly-license-and-operation" eller "yearly-maintenance"), så tilføj dem
# som nye tuples herunder.

FIELD_MAPPING = [
    # (geckoboard_field_id, geckoboard_type, geckoboard_label, podio_external_id)
    ("title", "string", "Titel", None),  # None = brug item's indbyggede titel
    ("status", "string", "Status", "status"),
    ("go_live_date", "date", "Go live 1st app", "go-live-1st-app"),
]


# ---------------------------------------------------------------------------
# Podio
# ---------------------------------------------------------------------------

def podio_authenticate() -> str:
    """Autentificér som Podio-app og returnér et access token.

    Bruger 'app authentication' som er beregnet til netop denne slags
    server-til-server integration mod én bestemt Podio-app.
    """
    resp = requests.post(
        f"{PODIO_API_BASE}/oauth/token",
        data={
            "grant_type": "app",
            "app_id": PODIO_APP_ID,
            "app_token": PODIO_APP_TOKEN,
            "client_id": PODIO_CLIENT_ID,
            "client_secret": PODIO_CLIENT_SECRET,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def podio_get_items(access_token: str) -> list[dict]:
    """Hent items fra Podio-appen (nyeste redigeret først)."""
    resp = requests.post(
        f"{PODIO_API_BASE}/item/app/{PODIO_APP_ID}/filter/",
        headers={"Authorization": f"Bearer {access_token}"},
        json={
            "limit": ITEM_LIMIT,
            "sort_by": "last_edit_on",
            "sort_desc": True,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["items"]


def _extract_field_value(item: dict, external_id: str):
    """Find rå-værdien for et Podio-felt ud fra dets external_id.

    Podio's felt-værdier er strukturerede forskelligt afhængig af felttype.
    Denne funktion dækker de mest almindelige typer; udvid selv efter behov
    (fx for 'app reference' eller 'contact' felter).
    """
    for field in item.get("fields", []):
        if field.get("external_id") != external_id:
            continue

        values = field.get("values", [])
        if not values:
            return None

        field_type = field.get("type")

        if field_type == "text":
            return values[0].get("value")
        if field_type == "number":
            return values[0].get("value")
        if field_type == "money":
            return values[0].get("value")
        if field_type == "category":
            return ", ".join(v["value"]["text"] for v in values)
        if field_type == "date":
            # start_date har formatet "YYYY-MM-DD" eller "YYYY-MM-DD HH:MM:SS"
            return values[0].get("start_date")
        if field_type == "app":
            return ", ".join(
                v["value"]["title"] for v in values if v.get("value")
            )
        if field_type == "contact":
            return ", ".join(
                v["value"]["name"] for v in values if v.get("value")
            )

        # Fallback: prøv at finde en "value" nøgle
        return values[0].get("value")

    return None


def build_rows(items: list[dict]) -> list[dict]:
    """Byg Geckoboard-rækker ud fra Podio-items via FIELD_MAPPING.

    Alle felter er markeret "optional" i dataset-skemaet (se
    geckoboard_ensure_dataset), så det er fint at sende None/null her for
    Podio-felter der ikke har en værdi på det enkelte item — Geckoboard vil
    bare vise dem som tomme.
    """
    rows = []
    for item in items:
        row = {}
        for field_id, _gtype, _label, podio_external_id in FIELD_MAPPING:
            if podio_external_id is None:
                row[field_id] = item.get("title")
            else:
                row[field_id] = _extract_field_value(item, podio_external_id)
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Geckoboard
# ---------------------------------------------------------------------------

def geckoboard_ensure_dataset():
    """Opret (eller opdatér) dataset-skemaet i Geckoboard.

    Dette er idempotent — det er fint at kalde det ved hver kørsel.
    """
    fields = {}
    for field_id, gtype, label, _ in FIELD_MAPPING:
        # "optional" skal være sat, ellers afviser Geckoboard rækker hvor
        # feltet er null eller helt udeladt — og Podio-felter er ofte tomme.
        field_def = {"type": gtype, "name": label, "optional": True}
        if gtype == "money":
            field_def["currency_code"] = GECKOBOARD_CURRENCY_CODE
        fields[field_id] = field_def

    resp = requests.put(
        f"{GECKOBOARD_API_BASE}/datasets/{GECKOBOARD_DATASET_NAME}",
        auth=(GECKOBOARD_API_KEY, ""),
        json={"fields": fields},
        timeout=30,
    )
    resp.raise_for_status()


def geckoboard_replace_data(rows: list[dict]):
    """Overskriv al data i datasettet med de nyeste rækker fra Podio.

    PUT erstatter hele datasettets indhold, hvilket er den nemmeste måde at
    holde det synkroniseret med Podio's aktuelle tilstand (ingen risiko for
    "duplikerede" eller forældede rækker fra tidligere kørsler).
    """
    resp = requests.put(
        f"{GECKOBOARD_API_BASE}/datasets/{GECKOBOARD_DATASET_NAME}/data",
        auth=(GECKOBOARD_API_KEY, ""),
        json={"data": rows},
        timeout=30,
    )
    resp.raise_for_status()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("Autentificerer mod Podio...")
    access_token = podio_authenticate()

    print(f"Henter items fra Podio-app {PODIO_APP_ID}...")
    items = podio_get_items(access_token)
    print(f"  -> {len(items)} items hentet")

    rows = build_rows(items)

    print(f"Sikrer Geckoboard-dataset '{GECKOBOARD_DATASET_NAME}' findes...")
    geckoboard_ensure_dataset()

    print(f"Pusher {len(rows)} rækker til Geckoboard...")
    geckoboard_replace_data(rows)

    print("Færdig ✅")


if __name__ == "__main__":
    try:
        main()
    except requests.HTTPError as exc:
        print(f"HTTP-fejl: {exc}", file=sys.stderr)
        print(f"Response body: {exc.response.text}", file=sys.stderr)
        sys.exit(1)
