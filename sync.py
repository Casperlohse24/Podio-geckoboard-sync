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
import time
from datetime import date

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
# Separat dataset der KUN indeholder projekter med en udfyldt go-live-dato
# ("leverede" projekter). Findes fordi Geckoboards widget-editor ikke tillader
# at filtrere på et felt der samtidig bruges som widgettets "Time value" —
# med kun to dato-felter i skemaet (sign-on/go-live) låser de hinanden fast,
# så et rent, forfiltreret dataset er den robuste løsning i stedet for at
# kæmpe med Geckoboards filter-UI.
GECKOBOARD_DELIVERED_DATASET_NAME = os.environ.get(
    "GECKOBOARD_DELIVERED_DATASET_NAME", f"{GECKOBOARD_DATASET_NAME}.delivered"
)
# Geckoboard kræver en valutakode (ISO 4217, fx "DKK", "EUR", "USD") for
# felter af typen "money".
GECKOBOARD_CURRENCY_CODE = os.environ.get("GECKOBOARD_CURRENCY_CODE", "DKK")

PODIO_API_BASE = "https://api.podio.com"
GECKOBOARD_API_BASE = "https://api.geckoboard.com"

# Hvor mange items der maks hentes pr. sync. Geckoboard datasets har et hårdt
# loft på 5.000 rækker pr. dataset, så det er også loftet her som standard.
# Podio's Item Filter API tillader op til 500 items pr. kald, men et så
# stort svar (flere MB) er i praksis skrøbeligt over netværket, så
# podio_get_items() paginerer (offset) i mindre, mere robuste sider.
ITEM_LIMIT = int(os.environ.get("PODIO_ITEM_LIMIT", "5000"))
PODIO_PAGE_SIZE = 200  # holder hvert enkelt Podio-kald hurtigt og stabilt
GECKOBOARD_PAGE_SIZE = 500  # Geckoboards maksimale antal rækker pr. PUT/POST-kald


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
# Mapping herunder er til Podio-appen "Projects" (App ID 3276523) og er
# bygget til at vise "tid fra kunde til levering":
#   - "title"          (Podio's indbyggede item-titel)
#   - "customer_name"  (app-reference felt, external_id "group" ->
#                        henter titlen på det tilknyttede Customers-item)
#   - "signed_on"      (date felt, external_id "contract-date",
#                        Podio-label "Dashboard: Order signing")
#   - "go_live_date"   (date felt, external_id "go-live-1st-app")
#   - "status"         (category felt, external_id "status") — bruges til at
#                        filtrere "aktive, venter på go-live"-listen, se
#                        ACTIVE_STATUSES nedenfor.
#
# "lead_time_days" beregnes automatisk nedenfor (go_live_date - signed_on)
# og er IKKE en del af denne liste — den tilføjes særskilt i
# geckoboard_ensure_dataset() og build_rows(), da den ikke kommer direkte
# fra ét Podio-felt.
#
# Skal du tilføje flere kolonner (fx money-felter som
# "monthly-license-and-operation" eller "yearly-maintenance"), så tilføj dem
# som nye tuples herunder.

FIELD_MAPPING = [
    # (geckoboard_field_id, geckoboard_type, geckoboard_label, podio_external_id)
    ("title", "string", "Projekt", None),  # None = brug item's indbyggede titel
    ("customer_name", "string", "Kunde", "group"),
    ("signed_on", "date", "Sign-on dato", "contract-date"),
    ("go_live_date", "date", "Go-live dato", "go-live-1st-app"),
    ("status", "string", "Status", "status"),
]

# Beregnet felt: antal dage fra sign-on til go-live. Vises som et separat
# "number"-felt i Geckoboard, så I kan plotte det som linjediagram over tid
# (X-akse: go_live_date) og se om leveringstiden falder.
LEAD_TIME_FIELD_ID = "lead_time_days"

# Statusser der reelt betyder "projektet er afsluttet" (leveret, annulleret
# eller overdraget til Customer Care) — IKKE "aktivt og venter på go-live".
# I praksis er langt de fleste projekter uden go-live-dato markeret "Done"
# (leveret, men uden at nogen fik skrevet go-live-datoen ind i Podio) eller
# "Cancelled" — kun de statusser der IKKE er i denne liste tæller reelt som
# aktive/ventende i DAYS_WAITING_FIELD_ID nedenfor.
TERMINAL_STATUSES = {"Done", "Cancelled", "Handover Customer Care"}

# Beregnet felt: antal dage siden sign-on for projekter der IKKE har en
# go-live-dato endnu — dvs. hvor længe et aktivt projekt allerede har
# ventet på levering. Bruges til et Leaderboard der viser hvem der har
# ventet længst lige nu (komplementerer lead_time_days, som kun findes for
# allerede afsluttede projekter).
DAYS_WAITING_FIELD_ID = "days_since_signed"


# ---------------------------------------------------------------------------
# Podio
# ---------------------------------------------------------------------------

def _post_with_retry(url, retries=3, backoff=5, **kwargs):
    """requests.post() med et par forsøg og stigende ventetid.

    Podio's API kan lejlighedsvis svare langsomt eller helt droppe
    forbindelsen på tunge kald (fx en fuld side på hundredvis af items).
    Da scriptet kører uovervåget hvert 5. minut via cron, er det bedre at
    prøve et par gange end at lade hele synkroniseringen fejle på en enkelt
    forbigående timeout.
    """
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            return requests.post(url, **kwargs)
        except requests.exceptions.RequestException as exc:
            last_exc = exc
            if attempt < retries:
                wait = backoff * attempt
                print(f"  ⚠️  {url} fejlede ({exc.__class__.__name__}), prøver igen om {wait}s...")
                time.sleep(wait)
    raise last_exc


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
    """Hent items fra Podio-appen (nyeste redigeret først), pagineret.

    Podio's Item Filter API tillader max 500 items pr. kald ("limit"), så
    her hentes side for side (via "offset") indtil enten alle items i
    appen er hentet, eller ITEM_LIMIT er nået (Geckoboards eget loft på
    5.000 rækker pr. dataset).
    """
    items = []
    offset = 0
    total = None
    page_num = 0
    while len(items) < ITEM_LIMIT:
        page_limit = min(PODIO_PAGE_SIZE, ITEM_LIMIT - len(items))
        page_num += 1
        print(f"  -> henter side {page_num} (offset {offset}, op til {page_limit} items)...")
        resp = _post_with_retry(
            f"{PODIO_API_BASE}/item/app/{PODIO_APP_ID}/filter/",
            headers={"Authorization": f"Bearer {access_token}"},
            json={
                "limit": page_limit,
                "offset": offset,
                "sort_by": "last_edit_on",
                "sort_desc": True,
            },
            # En side kan fylde flere MB og tage 10+ sek. at hente, så
            # timeout skal være rundhåndet (ellers ReadTimeout).
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        page_items = data["items"]
        total = data.get("total", total)
        items.extend(page_items)
        offset += len(page_items)

        if not page_items or (total is not None and offset >= total):
            break  # ingen flere sider

    if total is not None and len(items) < total:
        print(
            f"  ⚠️  Podio-appen har {total} items i alt, men kun "
            f"{len(items)} blev synkroniseret (ITEM_LIMIT={ITEM_LIMIT}). "
            "Sæt miljøvariablen PODIO_ITEM_LIMIT højere hvis alle skal med "
            "(Geckoboards eget loft er 5.000 rækker pr. dataset)."
        )

    return items


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


def _days_between(start_str, end_str):
    """Antal dage mellem to "YYYY-MM-DD"-datoer, eller None hvis en af dem mangler."""
    if not start_str or not end_str:
        return None
    try:
        start = date.fromisoformat(start_str[:10])
        end = date.fromisoformat(end_str[:10])
    except ValueError:
        return None
    return (end - start).days


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

        row[LEAD_TIME_FIELD_ID] = _days_between(
            row.get("signed_on"), row.get("go_live_date")
        )

        # Kun relevant for et projekt der reelt ER aktivt: har en sign-on
        # dato, mangler en go-live-dato, OG status er ikke en af de
        # "afsluttede" statusser (se TERMINAL_STATUSES). Uden dette sidste
        # tjek ville denne liste være domineret af projekter markeret
        # "Done"/"Cancelled" for flere år siden, som bare aldrig fik
        # udfyldt go-live-datoen — ikke reelt aktivt arbejde.
        if (
            row.get("signed_on")
            and not row.get("go_live_date")
            and row.get("status") not in TERMINAL_STATUSES
        ):
            row[DAYS_WAITING_FIELD_ID] = _days_between(
                row["signed_on"], date.today().isoformat()
            )
        else:
            row[DAYS_WAITING_FIELD_ID] = None

        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Geckoboard
# ---------------------------------------------------------------------------

def _build_field_schema() -> dict:
    """Byg Geckoboards feltskema (fælles for alle datasets vi skriver til)."""
    fields = {}
    for field_id, gtype, label, _ in FIELD_MAPPING:
        # Alle felter undtagen det første er markeret optional, ellers
        # afviser Geckoboard rækker hvor feltet er null eller helt udeladt
        # — og Podio-felter er ofte tomme. Geckoboard kræver dog mindst ét
        # ikke-optional felt pr. dataset, så det første (typisk "title",
        # som altid har en værdi) er required.
        is_first_field = not fields
        field_def = {"type": gtype, "name": label, "optional": not is_first_field}
        if gtype == "money":
            field_def["currency_code"] = GECKOBOARD_CURRENCY_CODE
        fields[field_id] = field_def

    # Beregnede felter (se LEAD_TIME_FIELD_ID / DAYS_WAITING_FIELD_ID og
    # build_rows) — ikke en del af FIELD_MAPPING, da de ikke kommer direkte
    # fra ét Podio-felt.
    fields[LEAD_TIME_FIELD_ID] = {
        "type": "number",
        "name": "Dage fra sign-on til go-live",
        "optional": True,
    }
    fields[DAYS_WAITING_FIELD_ID] = {
        "type": "number",
        "name": "Dage siden sign-on (endnu ikke live)",
        "optional": True,
    }
    return fields


def geckoboard_ensure_dataset(dataset_name: str, fields: dict):
    """Opret (eller opdatér) et dataset-skema i Geckoboard.

    Dette er idempotent — det er fint at kalde det ved hver kørsel. Hvis
    skemaet er ændret siden sidst (nye/fjernede kolonner), afviser
    Geckoboard en simpel PUT med 409 Conflict ("different fields already
    exist"). I så fald sletter vi det gamle dataset og genopretter det med
    det nye skema, så scriptet ikke kræver manuel oprydning i Geckoboard.
    """
    dataset_url = f"{GECKOBOARD_API_BASE}/datasets/{dataset_name}"
    resp = requests.put(
        dataset_url,
        auth=(GECKOBOARD_API_KEY, ""),
        json={"fields": fields},
        timeout=30,
    )
    if resp.status_code == 409:
        print(
            f"  -> Skema for '{dataset_name}' er ændret siden sidst, "
            "genopretter dataset..."
        )
        requests.delete(
            dataset_url, auth=(GECKOBOARD_API_KEY, ""), timeout=30
        ).raise_for_status()
        resp = requests.put(
            dataset_url,
            auth=(GECKOBOARD_API_KEY, ""),
            json={"fields": fields},
            timeout=30,
        )
    resp.raise_for_status()


def geckoboard_replace_data(dataset_name: str, rows: list[dict]):
    """Overskriv al data i datasettet med de givne rækker.

    Geckoboard tillader max 500 rækker pr. PUT/POST-kald. Det første kald
    bruger PUT ("replace"), som rydder HELE datasettets tidligere indhold
    (ingen risiko for forældede/slettede rækker fra tidligere kørsler) og
    skriver den første side. Eventuelle resterende sider tilføjes bagefter
    med POST ("append") — da PUT'et lige har ryddet datasettet i denne
    samme kørsel, er der ingen risiko for dubletter.
    """
    data_url = f"{GECKOBOARD_API_BASE}/datasets/{dataset_name}/data"
    chunks = [
        rows[i : i + GECKOBOARD_PAGE_SIZE]
        for i in range(0, len(rows), GECKOBOARD_PAGE_SIZE)
    ] or [[]]  # tom liste hvis "rows" er tom, så datasettet stadig ryddes

    resp = requests.put(
        data_url, auth=(GECKOBOARD_API_KEY, ""), json={"data": chunks[0]}, timeout=30
    )
    resp.raise_for_status()

    for chunk in chunks[1:]:
        resp = requests.post(
            data_url, auth=(GECKOBOARD_API_KEY, ""), json={"data": chunk}, timeout=30
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
    fields = _build_field_schema()

    print(f"Sikrer Geckoboard-dataset '{GECKOBOARD_DATASET_NAME}' findes...")
    geckoboard_ensure_dataset(GECKOBOARD_DATASET_NAME, fields)

    print(f"Pusher {len(rows)} rækker til Geckoboard...")
    geckoboard_replace_data(GECKOBOARD_DATASET_NAME, rows)

    # Separat dataset med KUN leverede projekter (go-live-dato udfyldt) — se
    # kommentaren ved GECKOBOARD_DELIVERED_DATASET_NAME for hvorfor.
    delivered_rows = [r for r in rows if r.get("go_live_date")]
    print(
        f"Sikrer Geckoboard-dataset '{GECKOBOARD_DELIVERED_DATASET_NAME}' "
        f"findes ({len(delivered_rows)} leverede projekter)..."
    )
    geckoboard_ensure_dataset(GECKOBOARD_DELIVERED_DATASET_NAME, fields)
    geckoboard_replace_data(GECKOBOARD_DELIVERED_DATASET_NAME, delivered_rows)

    # Data-kvalitets-påmindelse: projekter markeret "Done"/"Handover Customer
    # Care" uden en go-live-dato er sandsynligvis reelt leverede, men mangler
    # bare at få skrevet datoen ind. At udfylde dem retroaktivt er den
    # hurtigste vej til en brugbar leveringstids-trend (se README).
    missing_go_live = sum(
        1
        for r in rows
        if not r.get("go_live_date")
        and r.get("status") in ("Done", "Handover Customer Care")
    )
    if missing_go_live:
        print(
            f"  ℹ️  {missing_go_live} projekter er markeret Done/Handover "
            "Customer Care men mangler en go-live-dato i Podio. Udfyldes de "
            "retroaktivt, vokser leveringstids-trenden markant."
        )

    print("Færdig ✅")


if __name__ == "__main__":
    try:
        main()
    except requests.HTTPError as exc:
        print(f"HTTP-fejl: {exc}", file=sys.stderr)
        print(f"Response body: {exc.response.text}", file=sys.stderr)
        sys.exit(1)
