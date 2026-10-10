"""
Mystore -> PowerOffice: sendte ordre blir fakturautkast (salgsordre) i PowerOffice.
Erstatning for iizy-integrasjonen. Egen fil - endrer IKKE sync.py.

  python mystore_til_po.py inspiser        -> viser raadata for nyeste sendte ordre (kun lesing)
  python mystore_til_po.py torr            -> viser hvilke ordre som VILLE blitt overfoert (kun lesing)
  python mystore_til_po.py kjor            -> oppretter fakturautkast (krever ORDRE_ENABLED=true)

Sikringer:
  - Skriver kun naar ORDRE_ENABLED=true.
  - Med KUN_DEMO=true (standard) nekter den aa skrive mot noe annet enn PowerOffice demo.
  - Hver ordre faar ExternalImportReference MYSTORE-<ordre-id>; ordre som allerede
    finnes i PowerOffice med den referansen hoppes over (ingen dubletter).
  - Mystore leses bare, ingenting skrives tilbake dit.
"""

import os
import sys
import json
import logging
import requests

from sync import mystore_headers, MYSTORE_BASE, po_headers, PO_BASE_URL, _navn

log = logging.getLogger("mystore_til_po")

SENDT_STATUS_ID = os.environ.get("MYSTORE_SENDT_STATUS_ID", "3")
ANTALL          = int(os.environ.get("ANTALL", "5"))           # maks ordre per kjoering
FRA_ORDRE_ID    = int(os.environ.get("FRA_ORDRE_ID", "0"))     # overfoer kun ordre med id >= denne
ORDRE_STATUS    = os.environ.get("PO_ORDRE_STATUS", "Draft")   # Draft = fakturautkast
# Vipps/cash-merket legges i Bestillingsnr. (PurchaseOrderReference) sammen med ordrens referanse
FRAKT_VAREKODE  = os.environ.get("FRAKT_VAREKODE", "")         # valgfri varekode for frakt i PO
ORDRE_ENABLED   = os.environ.get("ORDRE_ENABLED", "false").lower() == "true"
KUN_DEMO        = os.environ.get("KUN_DEMO", "true").lower() == "true"


def _tall(d: dict, *keys) -> float | None:
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return None


def _tekst(d: dict, *keys) -> str:
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return _navn(v).strip()
    return ""


# ─── Mystore (kun lesing) ───────────────────────────────────────────────────

def ms_sendte_ordre(antall_sider: int = 3) -> list[dict]:
    ordre = []
    for side in range(1, antall_sider + 1):
        r = requests.get(f"{MYSTORE_BASE}/orders", headers=mystore_headers(), timeout=30,
                         params={"page[size]": 50, "page[number]": side, "sort": "-created_at"})
        r.raise_for_status()
        data = r.json().get("data", [])
        if not data:
            break
        for o in data:
            sid = (((o.get("relationships") or {}).get("order-status") or {}).get("data") or {}).get("id")
            if str(sid) == SENDT_STATUS_ID and int(o["id"]) >= FRA_ORDRE_ID:
                ordre.append(o)
    return ordre


def ms_ordre_detalj(ordre_id) -> tuple[dict, list, list]:
    # Proever aa ta med kommentarer; faller tilbake til bare produkter om Mystore ikke stoetter det
    r = requests.get(f"{MYSTORE_BASE}/orders/{ordre_id}", headers=mystore_headers(), timeout=30,
                     params={"include": "order-products,order-comments"})
    if r.status_code != 200:
        r = requests.get(f"{MYSTORE_BASE}/orders/{ordre_id}", headers=mystore_headers(), timeout=30,
                         params={"include": "order-products"})
    r.raise_for_status()
    data = r.json()
    attrs = (data.get("data") or {}).get("attributes", {})
    inc = data.get("included", [])
    linjer = [{"id": i.get("id"), **i.get("attributes", {})}
              for i in inc if str(i.get("type", "")).startswith("order-product")]
    return attrs, linjer, inc


# ─── PowerOffice ─────────────────────────────────────────────────────────────

def po_get_alle(sti: str, params: dict | None = None) -> list:
    rows, url, first = [], f"{PO_BASE_URL}{sti}", True
    while url:
        r = requests.get(url, headers=po_headers(), params=params if first else None, timeout=60)
        first = False
        if r.status_code == 204 or not r.text.strip():
            break
        r.raise_for_status()
        d = r.json()
        rows += d if isinstance(d, list) else (d.get("data") or d.get("value") or [])
        url = None
        pag = r.headers.get("x-pagination")
        if pag:
            try:
                url = json.loads(pag).get("nextPageLink")
            except ValueError:
                pass
    return rows


def po_eksisterende_refs() -> set:
    return {str(o.get("ExternalImportReference")) for o in po_get_alle("/SalesOrders")
            if o.get("ExternalImportReference")}


def po_varekoder() -> set:
    return {str(p.get("Code")).strip() for p in po_get_alle("/Products") if p.get("Code")}


def po_finn_eller_lag_kunde(o: dict, skriv: bool) -> tuple[int | None, str]:
    orgnr = _tekst(o, "company_number").replace(" ", "")
    epost = _tekst(o, "customer_address_email", "billing_address_email", "email")
    navn = (_tekst(o, "billing_address_company", "customer_address_company", "shipping_address_company")
            or _tekst(o, "billing_address_name", "customer_address_name", "shipping_address_name")
            or epost or "Ukjent kunde")

    treff = []
    if orgnr:
        treff = po_get_alle("/Customers", {"organizationNumbers": orgnr})
    if not treff and epost:
        treff = po_get_alle("/Customers", {"emailAddresses": epost})
    if treff:
        return int(treff[0]["Id"]), f"finnes: {treff[0].get('Name')}"

    if not skriv:
        return None, f"NY kunde: {navn} (org.nr {orgnr or '-'}, {epost or '-'})"

    payload = {
        "Name": navn[:200],
        "OrganizationNumber": orgnr or None,
        "EmailAddress": epost or None,
        "InvoiceEmailAddress": epost or None,
        "PhoneNumber": _tekst(o, "customer_address_phone", "billing_address_phone", "shipping_address_phone") or None,
        "MailAddress": {
            "AddressLine1": _tekst(o, "billing_address_address", "customer_address_address", "shipping_address_address"),
            "ZipCode": _tekst(o, "billing_address_zipcode", "customer_address_zipcode", "shipping_address_zipcode"),
            "City": _tekst(o, "billing_address_city", "customer_address_city", "shipping_address_city"),
            "CountryCode": "NO",
        },
    }
    payload = {k: v for k, v in payload.items() if v is not None}
    r = requests.post(f"{PO_BASE_URL}/Customers", headers=po_headers(), json=payload, timeout=30)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Kunne ikke opprette kunde {navn}: {r.status_code} {r.text[:300]}")
    return int(r.json()["Id"]), f"opprettet: {navn}"


def po_finn_eller_lag_prosjekt(navn: str, kunde_id, skriv: bool) -> tuple[int | None, str]:
    """Mystore-referansen blir prosjekt i PowerOffice (finnes det fra foer, gjenbrukes det)."""
    navn = navn.strip()[:100]
    for p in po_get_alle("/Projects", {"name": navn}):
        if str(p.get("Name", "")).strip().lower() == navn.lower():
            return int(p["Id"]), f"prosjekt finnes: {navn}"
    if not skriv:
        return None, f"NYTT prosjekt: {navn}"
    payload = {"Name": navn}
    if kunde_id:
        payload["CustomerId"] = kunde_id
    r = requests.post(f"{PO_BASE_URL}/Projects", headers=po_headers(), json=payload, timeout=30)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Kunne ikke opprette prosjekt {navn}: {r.status_code} {r.text[:300]}")
    return int(r.json()["Id"]), f"prosjekt opprettet: {navn}"


def kommentar(o: dict, inc: list) -> str:
    """Kundens/butikkens kommentar paa ordren (feltet 'Kommentar' i Mystore)."""
    tekst = _tekst(o, "comment", "comments", "customer_comment", "order_comment", "note", "notes", "message")
    if not tekst:
        deler = [_tekst(i.get("attributes", {}), "comment", "text", "message", "body", "content")
                 for i in inc if "comment" in str(i.get("type", ""))]
        tekst = "\n".join(d for d in deler if d)
    return tekst.strip()


def betalingsmerke(o: dict, inc: list) -> str:
    """Vipps/cash-merke. Feltnavn bekreftes med 'inspiser'."""
    # Mystore: payment_module = teknisk navn (invoice, vipps, klarna ...), payment_method = visningsnavn
    tekst = (_tekst(o, "payment_module") + " " + _tekst(o, "payment_method")).lower().strip()
    if not tekst:
        for i in inc:
            if "payment" in str(i.get("type", "")):
                tekst = _tekst(i.get("attributes", {}), "name", "title", "type").lower()
    if "vipps" in tekst:
        return "Vipps"
    if any(k in tekst for k in ("kort", "card", "kontant", "cash", "klarna", "nets", "stripe")):
        return "cash"
    return ""


def bygg_ordre(oid, o: dict, linjer: list, inc: list, kunde_id, varekoder: set, prosjekt_id=None) -> dict:
    # Som iizy: tekstlinje "Ordre <nr>" oeverst
    po_linjer = [{"LineType": "Text", "Description": f"Ordre {oid}"}]
    # Kommentaren fra Mystore som tekstlinje(r) rett under (maks 400 tegn per linje)
    for avsnitt in kommentar(o, inc).splitlines():
        avsnitt = avsnitt.strip()
        while avsnitt:
            po_linjer.append({"LineType": "Text", "Description": avsnitt[:400]})
            avsnitt = avsnitt[400:]
    for l in linjer:
        kode = _tekst(l, "sku", "product_sku")
        antall = _tall(l, "quantity", "qty") or 1
        # Som iizy: veiledende pris + forhandlerrabatt i %, kostpris paa linjen.
        #   original_price_without_dealer = veiledende pris eks. mva (100)
        #   final_price                   = det kunden betaler eks. mva (80) -> rabatt 20 %
        netto = _tall(l, "final_price", "price")
        brutto = _tall(l, "original_price_without_dealer", "original_price") or netto
        kost = _tall(l, "cost")
        mva = _tall(l, "tax_rate")
        linje = {"Quantity": antall, "Description": _tekst(l, "name", "title")[:400]}
        if brutto is not None:
            linje["ProductUnitPrice"] = brutto
            if netto is not None and brutto > 0 and netto < brutto:
                linje["Allowance"] = round((1 - netto / brutto) * 100, 4)
        if kost:
            linje["ProductUnitCost"] = kost
        if mva is not None and mva != 25 and not (kode and kode in varekoder):
            log.warning("Linje %s har %s %% mva men finnes ikke i PO - havner paa standard salgskonto", kode, mva)
        if kode and kode in varekoder:
            linje["ProductCode"] = kode
        else:
            linje["UseStandardSalesAccount"] = True
            if kode:
                linje["Description"] = f"{kode} {linje['Description']}"[:400]
        po_linjer.append(linje)

    frakt = _tall(o, "shipping_price_ex_vat", "shipping_cost_ex_vat", "shipping_price", "shipping_cost", "freight")
    if frakt:
        fl = {"Quantity": 1, "Description": "Frakt", "ProductUnitPrice": frakt}
        if FRAKT_VAREKODE and FRAKT_VAREKODE in varekoder:
            fl["ProductCode"] = FRAKT_VAREKODE
        else:
            fl["UseStandardSalesAccount"] = True
        po_linjer.append(fl)

    payload = {
        "CustomerId": kunde_id,
        "SalesOrderStatus": ORDRE_STATUS,
        "ExternalImportReference": f"MYSTORE-{oid}",
        "ImportedOrderNo": int(oid),
        "SalesOrderDate": (_tekst(o, "created_at") or "")[:10] or None,
        "DeliveryDate": __import__("datetime").date.today().isoformat(),
        # Som iizy: kontaktperson i "Deres referanse"
        "CustomerReference": _tekst(o, "customer_address_name", "billing_address_name")[:100] or None,
        "SalesOrderLines": po_linjer,
    }
    # Referansen i Mystore gaar til Prosjekt; Bestillingsnr. faar Vipps/cash-merket
    if prosjekt_id:
        payload["ProjectId"] = prosjekt_id
    merke = betalingsmerke(o, inc)
    if merke:
        payload["PurchaseOrderReference"] = merke
    return {k: v for k, v in payload.items() if v is not None}


# ─── Moduser ────────────────────────────────────────────────────────────────

def inspiser():
    ordre = ms_sendte_ordre(1)
    if not ordre:
        print("Fant ingen sendte ordre.")
        return
    oid = ordre[0]["id"]
    attrs, linjer, inc = ms_ordre_detalj(oid)
    print(f"=== Mystore ordre {oid}: ordrefelter ===")
    print(json.dumps(attrs, indent=2, ensure_ascii=False)[:6000])
    print("\n=== Inkluderte objekter (typer) ===")
    print(sorted({i.get("type") for i in inc}))
    print(f"\nReferanse (-> prosjekt): {_tekst(attrs, 'reference') or '-'}")
    print(f"Kommentar (-> tekstlinje): {kommentar(attrs, inc) or '-'}")
    if linjer:
        print("\n=== Foerste ordrelinje ===")
        print(json.dumps(linjer[0], indent=2, ensure_ascii=False)[:3000])


def overfor(skriv: bool):
    if skriv:
        if not ORDRE_ENABLED:
            log.error("ORDRE_ENABLED er ikke true - ingenting skrives.")
            sys.exit(1)
        if KUN_DEMO and "/demo/" not in PO_BASE_URL.lower():
            log.error("KUN_DEMO er paa, men PO_BASE_URL er ikke demo (%s) - stopper.", PO_BASE_URL)
            sys.exit(1)

    print(f"PowerOffice: {PO_BASE_URL}   Modus: {'SKRIV' if skriv else 'TORRKJORING'}")
    refs = po_eksisterende_refs()
    varekoder = po_varekoder()
    ordre = [o for o in ms_sendte_ordre() if f"MYSTORE-{o['id']}" not in refs][:ANTALL]
    print(f"{len(ordre)} sendte ordre som ikke finnes i PowerOffice (maks {ANTALL}).\n")

    ok = feil = 0
    for o in ordre:
        oid = o["id"]
        try:
            attrs, linjer, inc = ms_ordre_detalj(oid)
            kunde_id, kunde_info = po_finn_eller_lag_kunde(attrs, skriv)
            prosjekt_id, prosjekt_info = None, "-"
            if _tekst(attrs, "reference"):
                prosjekt_id, prosjekt_info = po_finn_eller_lag_prosjekt(_tekst(attrs, "reference"), kunde_id, skriv)
            payload = bygg_ordre(oid, attrs, linjer, inc, kunde_id, varekoder, prosjekt_id)
            sum_eks = sum((l.get("ProductUnitPrice") or 0) * l.get("Quantity", 0) * (1 - (l.get("Allowance") or 0) / 100)
                          for l in payload["SalesOrderLines"])
            print(f"Ordre {oid}: {kunde_info} | {len(payload['SalesOrderLines'])} linjer | "
                  f"netto {sum_eks:,.2f} kr | {prosjekt_info} | bestillingsnr: {payload.get('PurchaseOrderReference', '-')}")
            for l in payload["SalesOrderLines"]:
                if l.get("LineType") == "Text":
                    print(f"    (tekst)          {l['Description']}")
                    continue
                print(f"    {l.get('ProductCode', '(fritekst)'):<16} {l['Quantity']:>6} x "
                      f"{l.get('ProductUnitPrice', 0):>10,.2f} -{l.get('Allowance', 0):>5.1f} %  "
                      f"kost {l.get('ProductUnitCost', 0):>9,.2f}  {l['Description'][:45]}")
            if not skriv:
                continue
            r = requests.post(f"{PO_BASE_URL}/SalesOrders/Complete", headers=po_headers(),
                              json=payload, timeout=60)
            if r.status_code in (200, 201):
                print(f"    -> fakturautkast opprettet: {r.text[:120]}")
                ok += 1
            else:
                print(f"    -> FEIL {r.status_code}: {r.text[:400]}")
                feil += 1
        except Exception as e:
            print(f"Ordre {oid}: FEIL {e}")
            feil += 1

    print(f"\nFerdig: {ok} opprettet, {feil} feil." if skriv else "\nTORRKJORING - ingenting er sendt.")
    if feil:
        sys.exit(1)


if __name__ == "__main__":
    modus = sys.argv[1] if len(sys.argv) > 1 else "torr"
    if modus == "inspiser":
        inspiser()
    else:
        overfor(skriv=(modus == "kjor"))
