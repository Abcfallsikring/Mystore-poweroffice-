"""
MyStore → PowerOffice produktsynk
Synkroniserer antall (lager), innpris og utpris for alle produkter.

Kjøres automatisk via GitHub Actions (se .github/workflows/sync.yml).
Test:  python sync.py test   (viser rådata fra begge API-er)
Synk:  python sync.py

Nødvendige GitHub Secrets:
  MYSTORE_TOKEN           – API-token fra MyStore (samme som mystore-onix)
  MYSTORE_PRODUKT_TOKEN   – (valgfri) egen token for produkt-API, som i mystore-onix
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

# Synkroniser lagerantall. Krever at varen settes som lagervare (IsStockItem)
# i PowerOffice - da blir lagerfeltene ogsaa synlige i GUI.
# Sett SYNC_STOCK=false for aa synke kun priser.
SYNC_STOCK = os.environ.get("SYNC_STOCK", "true").lower() != "false"

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
    raw_mode=True returnerer uflatede attributter (for test).
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
            code = p.get("Code") or p.get("code") or p.get("articleNumber")
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

def po_update_product(po_product_id: str, product: dict, ms: dict) -> bool:
    """Oppdaterer pris OG lager i ett RFC 6902 JSON Patch-kall.

    Feltnavn hentet fra ProductPatchDto i PowerOffice v2 OpenAPI-spec:
      Name        = produktnavn (maks 400 tegn)
      UnitPrice   = utpris (salgspris)
      UnitCost    = innpris (kostpris)
      StockOnHand = antall paa lager
      IsStockItem = maa vaere true for at StockOnHand kan settes
    """
    patch_ops = []

    # Navn: MyStore er fasit. Oppdaterer kun naar navnet faktisk avviker,
    # slik at vi slipper unoedvendige skrivinger.
    navn = ms["name"].strip()[:400]
    if navn and navn != (product.get("Name") or ""):
        patch_ops.append({"op": "replace", "path": "/Name", "value": navn})

    if ms["price"] > 0:
        patch_ops.append({"op": "replace", "path": "/UnitPrice",
                          "value": ms["price"]})
    if ms["purchasePrice"] > 0:
        patch_ops.append({"op": "replace", "path": "/UnitCost",
                          "value": ms["purchasePrice"]})

    # Lager: PowerOffice krever IsStockItem=true foer StockOnHand kan settes.
    if SYNC_STOCK:
        if not product.get("IsStockItem"):
            patch_ops.append({"op": "replace", "path": "/IsStockItem",
                              "value": True})
        patch_ops.append({"op": "replace", "path": "/StockOnHand",
                          "value": ms["stockQuantity"]})

    if not patch_ops:
        return True

    resp = requests.patch(
        f"{PO_BASE_URL}/products/{po_product_id}",
        headers=po_headers(),
        json=patch_ops,
        timeout=15,
    )
    if resp.status_code in (200, 204):
        return True
    log.warning("PowerOffice PATCH produkt %s feil %s: %s",
                po_product_id, resp.status_code, resp.text)
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
        article_no = ms["articleNumber"]

        po = po_products.get(article_no)
        if not po:
            log.debug("Ikke funnet i PO: varenr %s (%s)", article_no, ms["name"])
            not_found += 1
            continue

        po_id = str(po.get("Id") or po.get("id"))

        if po_update_product(po_id, po, ms):
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

    log.info("--- TEST: Enkeltprodukt 281 (alle felter) ---")
    r = requests.get(f"{MYSTORE_BASE}/products/281", headers=mystore_headers(), timeout=30)
    print(f"GET /products/281 -> {r.status_code}")
    if r.status_code == 200:
        print(json.dumps(r.json(), indent=2, ensure_ascii=False)[:4000])

    log.info("--- TEST: Sonderer mulige lager-endepunkter ---")
    for path in ("products/281/stock", "stocks", "product-stocks",
                 "stock-groups", "stock-groups/1", "warehouses"):
        r = requests.get(f"{MYSTORE_BASE}/{path}", headers=mystore_headers(),
                         params={"page[size]": 2}, timeout=30)
        print(f"GET /{path} -> {r.status_code}")
        if r.status_code == 200:
            print(json.dumps(r.json(), indent=2, ensure_ascii=False)[:2500])

    log.info("--- TEST: Henter produkter fra PowerOffice ---")
    po = po_get_all_products()
    for key, val in list(po.items())[:3]:
        print(json.dumps(val, indent=2, ensure_ascii=False))
    log.info("PowerOffice totalt: %d produkter", len(po))


# ─── SEED-modus (opprett testvarer i PowerOffice demo) ──────────────────────

def seed_demo():
    """Oppretter noen testvarer i PowerOffice (demo) med varenummer fra MyStore,
    slik at synken har noe å matche mot."""
    log.info("--- SEED: Henter MyStore-produkter ---")
    products = mystore_get_all_products()
    kandidater = [p for p in products if p["stockQuantity"] > 0 and p["price"] > 0][:3]

    for ms in kandidater:
        payload = {
            "code": ms["articleNumber"],
            "name": ms["name"][:400],
            "salesPrice": ms["price"],
            "costPrice": ms["purchasePrice"],
            "standardSalesAccount": 3000,  # standard salgskonto (avgiftspliktig)
        }
        resp = requests.post(
            f"{PO_BASE_URL}/products",
            headers=po_headers(),
            json=payload,
            timeout=30,
        )
        print(f"POST /products {ms['articleNumber']} -> {resp.status_code}")
        print(resp.text[:400])


# ─── DIAG-modus (verifiser resultat i PowerOffice) ──────────────────────────

def diag_mode():
    """Viser status i PowerOffice + undersoeker enhet-felt og nullsaldo."""
    po = po_get_all_products()
    if not po:
        log.error("DIAG: ingen produkter i PowerOffice")
        return

    print(f"{'Varenr':<14} {'Utpris':>9} {'Innpris':>9} {'Lager':>7} {'Enhet':>6}  Navn")
    print("-" * 110)
    for kode, p in po.items():
        print(f"{kode:<14} "
              f"{str(p.get('UnitPrice')):>9} "
              f"{str(p.get('UnitCost')):>9} "
              f"{str(p.get('StockOnHand')):>7} "
              f"{str(p.get('UnitOfMeasureCode')):>6}  "
              f"{p.get('Name')}")
    print(f"\nTotalt {len(po)} produkter i PowerOffice.\n")

    # ── 1) Hvorfor matcher ikke enkelte varer? ────────────────────────────
    print("=" * 70)
    print("DIAG A: Leter etter varer som finnes i PO men ikke matcher MyStore")
    print("=" * 70)

    ms = mystore_get_all_products()
    ms_sku = {p["articleNumber"] for p in ms}
    umatchet = [k for k in po if k not in ms_sku]

    print(f"MyStore: {len(ms)} varer.  PowerOffice: {len(po)} varer.")
    print(f"Uten match i MyStore: {umatchet}\n")

    # Direkte oppslag paa kjent vare (G-1179-S/M har MyStore-id 566)
    print("Direkte oppslag GET /products/566:")
    d = requests.get(f"{MYSTORE_BASE}/products/566",
                     headers=mystore_headers(), timeout=30)
    print(f"  HTTP {d.status_code}")
    if d.status_code == 200:
        a = d.json().get("data", {}).get("attributes", {})
        print(f"  sku    = {a.get('sku')!r}")
        print(f"  navn   = {_navn(a.get('name'))!r}")
        print(f"  status = {a.get('status')!r}")
        print(f"  pris   = {a.get('price')!r}  kost = {a.get('cost')!r}")
        print(f"  lager  = {a.get('quantity_physical')!r}")
        print(f"  Var 566 med i listen paa {len(ms)} varer? "
              f"{'JA' if str(a.get('sku')).strip() in ms_sku else 'NEI'}")
    else:
        print(f"  {d.text[:300]}")
    print()

    # Hvor mange MyStore-varer mangler SKU helt?
    tomme = [p for p in ms if not p["articleNumber"].isprintable()
             or p["articleNumber"].isdigit()]
    print(f"MyStore-varer der SKU er tom (faller tilbake paa intern id): "
          f"{len(tomme)}")
    if tomme[:5]:
        print(f"  eksempler: {[p['articleNumber'] for p in tomme[:5]]}\n")

    # Finnes de umatchede kodene i raadata - som sku, ean eller variant?
    print("Soeker etter kodene i MyStore raadata (sku / ean / navn):")
    raa = mystore_get_all_products(raw_mode=True)
    for kode in umatchet:
        treff = []
        for item in raa:
            a = item.get("attributes", {})
            felter = {
                "sku": a.get("sku"),
                "ean": a.get("ean"),
                "manufacturer_sku": a.get("manufacturer_sku"),
                "navn": _navn(a.get("name")),
            }
            for fnavn, verdi in felter.items():
                if verdi and kode.lower() in str(verdi).lower():
                    treff.append(f"id={item.get('id')} {fnavn}={verdi!r}")
        print(f"\n  {kode}:")
        if treff:
            for t in treff[:5]:
                print(f"    TREFF  {t}")
        else:
            print("    ingen treff i /products - ligger trolig som variant")

    # Har MyStore et eget variant-endepunkt?
    print("\nSonderer variant-endepunkter i MyStore:")
    for sti in ("variants", "product-variants", "products/variants"):
        v = requests.get(f"{MYSTORE_BASE}/{sti}", headers=mystore_headers(),
                         params={"page[size]": 2}, timeout=30)
        print(f"  GET /{sti} -> {v.status_code}")
        if v.status_code == 200:
            d = v.json().get("data", [])
            if d:
                print(f"    {json.dumps(d[0], ensure_ascii=False)[:500]}")

    # ── 2) KRITISK: nullstilles en vare som HAR lager? ────────────────────
    print()
    print("=" * 70)
    print("DIAG B: Gaar en vare fra positivt lager til 0?")
    print("=" * 70)

    vare = next((p for p in po.values()
                 if (p.get("StockOnHand") or 0) > 0), None)
    if not vare:
        print("Ingen vare med positivt lager - kan ikke teste nedtelling.")
        return

    pid = str(vare.get("Id"))
    start = vare.get("StockOnHand")
    print(f"Testvare: {vare.get('Code')} (id={pid}) startlager={start}\n")

    def les():
        v = requests.get(f"{PO_BASE_URL}/products/{pid}",
                         headers=po_headers(), timeout=15)
        return v.json() if v.status_code == 200 else {}

    for navn, verdi in [("Setter lager til 0", 0),
                        (f"Setter lager tilbake til {start}", start)]:
        r = requests.patch(
            f"{PO_BASE_URL}/products/{pid}",
            headers=po_headers(),
            json=[{"op": "replace", "path": "/StockOnHand", "value": verdi}],
            timeout=15,
        )
        merk = "OK" if r.status_code in (200, 204) else "FEIL"
        print(f"[{merk}] {navn} -> HTTP {r.status_code}")
        if r.status_code not in (200, 204) and r.text.strip():
            print(f"      {r.text[:300]}")
        o = les()
        print(f"      -> StockOnHand={o.get('StockOnHand')} "
              f"StockAvailable={o.get('StockAvailable')}")

    print("\nKONKLUSJON: hvis 'Setter lager til 0' ga StockOnHand=0.0 eller")
    print("None, nullstilles varen korrekt. Star den fortsatt paa "
          f"{start}, er dette en reell feil.")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "sync"
    if mode == "test":
        test_mode()
    elif mode == "seed":
        seed_demo()
    elif mode == "diag":
        diag_mode()
    else:
        run_sync()
