"""
MyStore → PowerOffice produktsynk
Synkroniserer antall (lager), innpris og utpris for alle produkter.

Kjøres automatisk via GitHub Actions (se .github/workflows/sync.yml).
Kan også kjøres manuelt: python sync.py

Nødvendige miljøvariabler (legg inn som GitHub Secrets):
  MYSTORE_TOKEN           – Personal Access Token fra MyStore admin
  MYSTORE_SHOP            – Butikknavnet ditt (f.eks. abcfallsikring)
  PO_APP_KEY              – Application Key fra PowerOffice developer-portal
  PO_CLIENT_KEY           – Client Key fra PowerOffice (genereres ved API-onboarding)
  PO_SUBSCRIPTION_KEY     – Subscription Key fra PowerOffice developer-portal
"""

import os
import sys
import base64
import logging
import requests
from datetime import datetime, timedelta

# ─── Logging ────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ─── Konfigurasjon ──────────────────────────────────────────────────────────

MYSTORE_API_KEY    = os.environ["MYSTORE_TOKEN"]         # same secret as mystore-onix repo
MYSTORE_STORE_NAME = os.environ["MYSTORE_SHOP"]          # same secret as mystore-onix repo
MYSTORE_BASE_URL   = "https://api.mystore.no/v1"

PO_APP_KEY         = os.environ["PO_APP_KEY"]
PO_CLIENT_KEY      = os.environ["PO_CLIENT_KEY"]
PO_SUBSCRIPTION_KEY= os.environ["PO_SUBSCRIPTION_KEY"]
# Demo/test-miljø (nøklene er demo-nøkler). Bytt til goapi.poweroffice.net for produksjon,
# eller sett PO_BASE_URL / PO_TOKEN_URL som miljøvariabler.
PO_BASE_URL        = os.environ.get("PO_BASE_URL", "https://goapitest.poweroffice.net/v2")
PO_TOKEN_URL       = os.environ.get("PO_TOKEN_URL", "https://goapitest.poweroffice.net/OAuth/Token")

# ─── MyStore: hent produkter ────────────────────────────────────────────────

def mystore_get_all_products() -> list[dict]:
    """
    Henter alle produkter fra MyStore inkl. lager og priser.
    """
    headers = {
        "Authorization": f"Bearer {MYSTORE_API_KEY}",
        "Accept": "application/json",
        "X-Store": MYSTORE_STORE_NAME,
    }

    products = []
    page = 1
    page_size = 100

    while True:
        resp = requests.get(
            f"{MYSTORE_BASE_URL}/products",
            headers=headers,
            params={
                "page": page,
                "pageSize": page_size,
                "includeStock": "true",
                "includePrice": "true",
            },
            timeout=30,
        )

        if resp.status_code != 200:
            log.error("MyStore API-feil %s: %s", resp.status_code, resp.text[:300])
            resp.raise_for_status()

        data = resp.json()

        # OBS: Feltnavnene er basert på MyStore sin CSV-eksport.
        # Kjør 'python sync.py test' for å se faktiske feltnavn og juster om nødvendig.
        batch = data.get("products") or data.get("data") or data
        if not batch:
            break

        for p in batch:
            products.append({
                "articleNumber": p.get("articleNumber") or p.get("sku") or p.get("id"),
                "name":          p.get("name") or p.get("productName", ""),
                "price":         float(p.get("price") or p.get("salesPrice") or 0),
                "purchasePrice": float(p.get("purchasePrice") or p.get("costPrice") or 0),
                "stockQuantity": int(p.get("physicalStock") or p.get("stockQuantity") or p.get("stock") or 0),
            })

        total = data.get("total") or data.get("totalCount") or len(batch)
        if page * page_size >= total:
            break
        page += 1

    log.info("MyStore: hentet %d produkter", len(products))
    return products


# ─── PowerOffice: autentisering ─────────────────────────────────────────────

_po_token = None
_po_token_expires = None


def po_get_token() -> str:
    global _po_token, _po_token_expires

    now = datetime.utcnow()
    if _po_token and _po_token_expires and now < _po_token_expires:
        return _po_token

    credentials = base64.b64encode(
        f"{PO_APP_KEY}:{PO_CLIENT_KEY}".encode()
    ).decode()

    resp = requests.post(
        PO_TOKEN_URL,
        headers={
            "Authorization": f"Basic {credentials}",
            "Ocp-Apim-Subscription-Key": PO_SUBSCRIPTION_KEY,
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data="grant_type=client_credentials",
        timeout=15,
    )

    if resp.status_code != 200:
        log.error("PowerOffice token-feil %s: %s", resp.status_code, resp.text[:300])
        resp.raise_for_status()

    token_data = resp.json()
    _po_token = token_data["access_token"]
    _po_token_expires = datetime.utcnow() + timedelta(
        seconds=token_data.get("expires_in", 1200) - 60
    )
    log.info("PowerOffice: nytt access token hentet")
    return _po_token


def po_headers() -> dict:
    return {
        "Authorization": f"Bearer {po_get_token()}",
        "Ocp-Apim-Subscription-Key": PO_SUBSCRIPTION_KEY,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


# ─── PowerOffice: hent produkter ────────────────────────────────────────────

def po_get_all_products() -> dict:
    """
    Returnerer en dict: { articleNumber -> produkt-dict fra PowerOffice }
    """
    products = {}
    skip = 0
    size = 100

    while True:
        resp = requests.get(
            f"{PO_BASE_URL}/products",
            headers=po_headers(),
            params={"$top": size, "$skip": skip},
            timeout=30,
        )

        if resp.status_code != 200:
            log.error("PowerOffice GET products feil %s: %s", resp.status_code, resp.text[:300])
            resp.raise_for_status()

        data = resp.json()
        if isinstance(data, list):
            batch = data
        else:
            batch = data.get("data") or []

        if not batch:
            break

        for p in batch:
            code = p.get("code") or p.get("articleNumber") or p.get("productCode")
            if code:
                products[str(code)] = p

        if len(batch) < size:
            break
        skip += size

    log.info("PowerOffice: hentet %d produkter", len(products))
    return products


# ─── PowerOffice: oppdater produkt ──────────────────────────────────────────

def po_update_product(po_product_id: str, payload: dict) -> bool:
    resp = requests.patch(
        f"{PO_BASE_URL}/products/{po_product_id}",
        headers=po_headers(),
        json=payload,
        timeout=15,
    )

    if resp.status_code in (200, 204):
        return True

    log.warning(
        "PowerOffice PATCH produkt %s feil %s: %s",
        po_product_id, resp.status_code, resp.text[:200],
    )
    return False


# ─── PowerOffice: oppdater lagerbeholdning ──────────────────────────────────

def po_set_stock(po_product_id: str, quantity: int) -> bool:
    """
    Setter lagerbeholdning via stock-endepunktet.
    """
    resp = requests.post(
        f"{PO_BASE_URL}/products/{po_product_id}/stockEntries",
        headers=po_headers(),
        json={
            "quantity": quantity,
            "entryType": "ManualAdjustment",
        },
        timeout=15,
    )

    if resp.status_code in (200, 201, 204):
        return True

    log.warning(
        "PowerOffice stock-oppdatering for %s feil %s: %s",
        po_product_id, resp.status_code, resp.text[:200],
    )
    return False


# ─── Hoved-synk ─────────────────────────────────────────────────────────────

def run_sync():
    log.info("=== Starter MyStore → PowerOffice synk ===")

    mystore_products = mystore_get_all_products()
    po_products = po_get_all_products()

    updated = 0
    not_found = 0
    errors = 0

    for ms in mystore_products:
        article_no = str(ms["articleNumber"])

        po = po_products.get(article_no)
        if not po:
            log.debug("Ikke funnet i PO: varenr %s (%s)", article_no, ms["name"])
            not_found += 1
            continue

        po_id = str(po.get("id") or po.get("productId"))

        price_payload = {}
        if ms["price"] > 0:
            price_payload["salesPrice"] = ms["price"]
        if ms["purchasePrice"] > 0:
            price_payload["purchasePrice"] = ms["purchasePrice"]

        price_ok = True
        if price_payload:
            price_ok = po_update_product(po_id, price_payload)

        stock_ok = po_set_stock(po_id, ms["stockQuantity"])

        if price_ok and stock_ok:
            log.info(
                "OK  %s  |  antall=%d  innpris=%.2f  utpris=%.2f",
                article_no, ms["stockQuantity"], ms["purchasePrice"], ms["price"],
            )
            updated += 1
        else:
            errors += 1

    log.info(
        "=== Ferdig: %d oppdatert, %d ikke funnet i PO, %d feil ===",
        updated, not_found, errors,
    )

    if errors > 0:
        sys.exit(1)


# ─── TEST-modus ─────────────────────────────────────────────────────────────

def test_mode():
    """
    Kjør med:  python sync.py test
    Skriver ut rådata fra begge API-er så du kan verifisere feltnavnene.
    """
    import json
    log.info("--- TEST: Henter 3 produkter fra MyStore ---")
    products = mystore_get_all_products()
    for p in products[:3]:
        print(json.dumps(p, indent=2, ensure_ascii=False))

    log.info("--- TEST: Henter 3 produkter fra PowerOffice ---")
    po = po_get_all_products()
    for key, val in list(po.items())[:3]:
        print(json.dumps(val, indent=2, ensure_ascii=False))


# ─── Inngangspunkt ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        test_mode()
    else:
        run_sync()
