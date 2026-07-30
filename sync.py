"""
MyStore → PowerOffice produktsynk
Synkroniserer antall (lager), innpris og utpris for alle produkter.

Kjøres automatisk via GitHub Actions (se .github/workflows/sync.yml).
Test:  python sync.py test   (viser rådata fra begge API-er)
Synk:  python sync.py

Nødvendige GitHub Secrets:
  MYSTORE_TOKEN           – API-token fra MyStore (samme som mystore-onix)
  MYSTORE_PRODUKT_TOKEN   – egen token for produkt-API uten hide-scopes (viser cost/quantity)
  MYSTORE_SHOP            – Butikknavn, f.eks. abcfallsikr202
  PO_APP_KEY              – Application Key fra PowerOffice developer-portal
  PO_CLIENT_KEY           – Client Key fra PowerOffice (API-onboarding)
  PO_SUBSCRIPTION_KEY     – Subscription Key fra PowerOffice developer-portal
"""

import os
import sys
import json
import base64
import logging
import requests
from datetime import datetime, timedelta

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ─── Konfigurasjon ──────────────────────────────────────────────────────────

MYSTORE_TOKEN = os.environ.get("MYSTORE_PRODUKT_TOKEN") or os.environ["MYSTORE_TOKEN"]
MYSTORE_SHOP  = os.environ["MYSTORE_SHOP"]
MYSTORE_BASE  = f"https://api.mystore.no/shops/{MYSTORE_SHOP}"

PO_APP_KEY          = os.environ["PO_APP_KEY"]
PO_CLIENT_KEY       = os.environ["PO_CLIENT_KEY"]
PO_SUBSCRIPTION_KEY = os.environ["PO_SUBSCRIPTION_KEY"]
# Demo/test-miljø (demo-nøkler). For produksjon: fjern 'demo/' og 'Demo/' fra URL-ene.
PO_BASE_URL  = os.environ.get("PO_BASE_URL", "https://goapi.poweroffice.net/demo/v2")
PO_TOKEN_URL = os.environ.get("PO_TOKEN_URL", "https://goapi.poweroffice.net/Demo/OAuth/Token")

# ─── MyStore (JSON:API, samme format som mystore-onix) ──────────────────────

def mystore_headers() -> dict:
    return {
        "Authorization": f"Bearer {MYSTORE_TOKEN}",
        "Content-Type": "application/vnd.api+json",
        "Accept": "application/vnd.api+json",
    }


def _navn(name_val) -> str:
    if isinstance(name_val, dict):
        return name_val.get("no") or name_val.get("en") or next(iter(name_val.values()), "") or ""
    return str(name_val or "")


def _tall(attrs: dict, *keys) -> float:
    for k in keys:
        v = attrs.get(k)
        if v is not None and v != "":
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return 0.0


def mystore_get_all_products(raw_mode: bool = False) -> list[dict]:
    """
    Henter alle produkter fra MyStore (JSON:API med paginering).
    raw_mode=True returnerer hele objektene (for test).
    """
    products = []
    page = 1

    while True:
        resp = requests.get(
            f"{MYSTORE_BASE}/products",
            headers=mystore_headers(),
            params={"page[size]": 100, "page[number]": page},
            timeout=30,
        )

        if resp.status_code != 200:
            log.error("MyStore API-feil %s: %s", resp.status_code, resp.text[:300])
            resp.raise_for_status()

        data = resp.json()
        raw = data.get("data", []) if isinstance(data, dict) else []
        if not raw:
            break

        for item in raw:
            attrs = item.get("attributes", {})
            if raw_mode:
                products.append(item)  # hele objektet inkl. relationships
                continue

            # Feltnavn verifisert mot MyStore API (krever token uten hide-scopes):
            #   price             = utpris
            #   cost              = innpris
            #   quantity_physical = fysisk lager
            products.append({
                "articleNumber": str(attrs.get("sku") or item.get("id")).strip(),
                "name":          _navn(attrs.get("name")),
                "price":         _tall(attrs, "price"),
                "purchasePrice": _tall(attrs, "cost"),
                "stockQuantity": int(_tall(attrs, "quantity_physical", "quantity")),
            })

        links = data.get("links", {}) if isinstance(data, dict) else {}
        if not links.get("next"):
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
    """Returnerer dict: { articleNumber -> produkt fra PowerOffice }.
    Standard sidestørrelse er 5000; paginering via x-pagination-header."""
    products = {}
    url = f"{PO_BASE_URL}/products"

    while url:
        resp = requests.get(url, headers=po_headers(), timeout=60)

        if resp.status_code == 204 or not resp.text.strip():
            # Ingen produkter (tomt miljø)
            break

        if resp.status_code != 200:
            log.error("PowerOffice GET products feil %s: %s", resp.status_code, resp.text[:300])
            resp.raise_for_status()

        data = resp.json()
        if isinstance(data, list):
            batch = data
        else:
            batch = data.get("data") or data.get("value") or []

        for p in batch:
            code = p.get("code") or p.get("articleNumber") or p.get("productCode")
            if code:
                products[str(code).strip()] = p

        # Neste side fra x-pagination-header (om datasettet er større enn sidestørrelsen)
        url = None
        pag = resp.headers.get("x-pagination")
        if pag:
            try:
                url = json.loads(pag).get("nextPageLink") or json.loads(pag).get("NextPageLink")
            except (ValueError, AttributeError):
                pass

    log.info("PowerOffice: hentet %d produkter", len(products))
    return products


# ─── PowerOffice: oppdater ──────────────────────────────────────────────────

def po_update_product(po_product_id: str, payload: dict) -> bool:
    resp = requests.patch(
        f"{PO_BASE_URL}/products/{po_product_id}",
        headers=po_headers(),
        json=payload,
        timeout=15,
    )
    if resp.status_code in (200, 204):
        return True
    log.warning("PowerOffice PATCH produkt %s feil %s: %s",
                po_product_id, resp.status_code, resp.text[:200])
    return False


def po_set_stock(po_product_id: str, quantity: int) -> bool:
    resp = requests.post(
        f"{PO_BASE_URL}/products/{po_product_id}/stockEntries",
        headers=po_headers(),
        json={"quantity": quantity, "entryType": "ManualAdjustment"},
        timeout=15,
    )
    if resp.status_code in (200, 201, 204):
        return True
    log.warning("PowerOffice stock-oppdatering for %s feil %s: %s",
                po_product_id, resp.status_code, resp.text[:200])
    return False


# ─── Hoved-synk ─────────────────────────────────────────────────────────────

def run_sync():
    log.info("=== Starter MyStore → PowerOffice synk ===")

    mystore_products = mystore_get_all_products()
    po_products = po_get_all_products()

    if not po_products:
        log.warning("PowerOffice har ingen produkter - ingenting aa oppdatere.")
        return

    updated = 0
    not_found = 0
    errors = 0

    for ms in mystore_products:
        article_no = ms["articleNumber"]

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
            log.info("OK  %s  |  antall=%d  innpris=%.2f  utpris=%.2f",
                     article_no, ms["stockQuantity"], ms["purchasePrice"], ms["price"])
            updated += 1
        else:
            errors += 1

    log.info("=== Ferdig: %d oppdatert, %d ikke funnet i PO, %d feil ===",
             updated, not_found, errors)

    if errors > 0:
        sys.exit(1)


# ─── TEST-modus ─────────────────────────────────────────────────────────────

def test_mode():
    """Viser rådata fra begge API-er slik at feltnavn kan verifiseres."""
    log.info("--- TEST: Token i bruk: %s ---",
             "MYSTORE_PRODUKT_TOKEN" if os.environ.get("MYSTORE_PRODUKT_TOKEN") else "MYSTORE_TOKEN")

    log.info("--- TEST: Henter alle MyStore-produkter (ferdig mappet) ---")
    products = mystore_get_all_products()
    med_lager = [p for p in products if p["stockQuantity"] > 0]
    med_innpris = [p for p in products if p["purchasePrice"] > 0]
    log.info("Totalt: %d | med lager > 0: %d | med innpris > 0: %d",
             len(products), len(med_lager), len(med_innpris))
    for p in (med_lager[:3] or products[:3]):
        print(json.dumps(p, indent=2, ensure_ascii=False))

    log.info("--- TEST: Henter produkter fra PowerOffice ---")
    po = po_get_all_products()
    for key, val in list(po.items())[:3]:
        print(json.dumps(val, indent=2, ensure_ascii=False))
    log.info("PowerOffice totalt: %d produkter", len(po))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        test_mode()
    else:
        run_sync()
