# Podio → Geckoboard sync

Henter items fra en Podio-app og pusher dem ind i et Geckoboard dataset via
Geckoboard's Dataset API. Kører automatisk hvert 5. minut via GitHub Actions,
så dit Geckoboard-widget altid viser (næsten) live data.

## Sådan virker det

1. GitHub Actions trigger `sync.py` hvert 5. minut (se `.github/workflows/sync.yml`).
2. Scriptet logger ind på Podio via "app authentication" og henter items fra den app du peger på.
3. Items mappes om til rækker ud fra `FIELD_MAPPING` i `sync.py`.
4. Rækkerne pushes til et Geckoboard dataset (skema oprettes/opdateres automatisk).
5. I Geckoboard bygger du et widget der trækker fra dette dataset — widgettet opdateres automatisk hver gang der pushes nyt data.

## 1. Opsætning af Podio-adgang

Du skal bruge 4 værdier fra Podio:

1. **Client ID + Client Secret**
   Gå til https://podio.com/settings/api og opret en ny API-nøgle (vælg en URL du kontrollerer, den bruges ikke aktivt af scriptet — fx `https://github.com`). Du får et Client ID og Client Secret.

2. **App ID + App Token**
   Gå ind i den Podio-app du vil trække data fra → **⋮ (flere muligheder)** → **Udvikler-information** ("Developer info"). Her finder du:
   - **App ID**
   - **App token**

   Disse to giver scriptet adgang til netop denne ene app, uden at skulle logge ind som en bruger.

## 2. Opsætning af Geckoboard

1. Log ind på Geckoboard → **Account settings** → **API** → kopiér din **API key**.
2. Vælg et navn til dit dataset. Dette repo bruger `podio.projects` (sat som `GECKOBOARD_DATASET_NAME`).

⚠️ Skifter du senere `FIELD_MAPPING` (tilføjer/fjerner kolonner), sletter og genopretter scriptet automatisk dataset'et med det nye skema (Geckoboard tillader ikke at ændre felter på et eksisterende dataset via en almindelig opdatering) — det er forventet og ikke en fejl, men betyder at dataset'et er tomt lige efter en skema-ændring, indtil næste kørsel har fyldt det igen.

## 3. Feltmapping

`FIELD_MAPPING` i [sync.py](sync.py) er sat op til Podio-appen **"Projects"** (App ID 3276523):

| Geckoboard-felt | Type | Podio external_id |
|---|---|---|
| `title` | string | (item's indbyggede titel — required) |
| `status` | string | `status` |
| `go_live_date` | date | `go-live-1st-app` |

Vil du tilføje flere kolonner, fx omsætning, er oplagte kandidater fra appen: `monthly-license-and-operation` og `yearly-maintenance` (begge type `money` — husk `GECKOBOARD_CURRENCY_CODE`, default `DKK`).

Find et felts `external_id`: åbn appen i Podio → **⋮ → Udvikler-information**, eller **Konfigurér felter** → klik på feltet. Geckoboard-typer: `string`, `number`, `date`, `datetime`, `money`, `percentage`, `boolean`.

Scriptet dækker de mest almindelige Podio felt-typer (tekst, tal, kategori, dato, app-reference, kontakt). Har du andre felttyper (fx "app reference" med flere niveauer), kan `_extract_field_value` i `sync.py` udvides.

Bemærk: Geckoboard kræver mindst ét ikke-valgfrit felt pr. dataset — det første felt i `FIELD_MAPPING` (`title`) er derfor altid required, resten er optional, så tomme Podio-felter ikke fejler synkroniseringen.

## 4. Læg secrets ind i GitHub

I dit GitHub-repo: **Settings → Secrets and variables → Actions → New repository secret**, og opret:

| Secret navn | Værdi |
|---|---|
| `PODIO_CLIENT_ID` | Client ID fra trin 1 |
| `PODIO_CLIENT_SECRET` | Client Secret fra trin 1 |
| `PODIO_APP_ID` | App ID fra trin 1 |
| `PODIO_APP_TOKEN` | App token fra trin 1 |
| `GECKOBOARD_API_KEY` | API key fra trin 2 |
| `GECKOBOARD_DATASET_NAME` | Dataset-navn, fx `podio.items` |

## 5. Push til GitHub og test

```bash
git remote add origin <din-repo-url>
git add -A
git commit -m "Initial Podio -> Geckoboard sync"
git push -u origin main
```

Gå derefter til **Actions**-fanen i GitHub, vælg "Sync Podio to Geckoboard", og klik **Run workflow** for at teste den manuelt (i stedet for at vente 5 minutter).

## 6. Test lokalt (valgfrit)

```bash
pip install -r requirements.txt

export PODIO_CLIENT_ID=...
export PODIO_CLIENT_SECRET=...
export PODIO_APP_ID=...
export PODIO_APP_TOKEN=...
export GECKOBOARD_API_KEY=...
export GECKOBOARD_DATASET_NAME=podio.items

python sync.py
```

## 7. Byg widget i Geckoboard

Når scriptet har kørt mindst én gang, kan du i Geckoboard oprette et nyt widget → vælg **Datasets** som kilde → vælg dit dataset (`podio.items`) → vælg visualisering (liste, tal, graf osv.).

## Om "live" opdatering

GitHub Actions' `schedule`-trigger kører **mindst** hvert 5. minut (kan ikke sættes hurtigere), og kan i praksis blive forsinket lidt ved høj belastning hos GitHub. Det giver et dashboard der opdateres praktisk talt live (typisk inden for 5-10 minutter efter en ændring i Podio).

Vil du senere have det tættere på ægte realtid, kan vi udvide løsningen med **Podio webhooks**, så en ændring i Podio sender besked med det samme i stedet for at vente på næste polling — sig til, så bygger vi det oven på denne løsning.
