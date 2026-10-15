"""
Mystore er fasit for varer: oppretter varer som finnes i Mystore men mangler i PowerOffice.
Egen fil - endrer IKKE sync.py. sync.py oppdaterer deretter antall og priser paa alle varer.

  python produkter_til_po.py torr   -> viser hvilke varer som mangler (kun lesing)
  python produkter_til_po.py kjor   -> oppretter dem (krever ORDRE_ENABLED=true, og demo naar KUN_DEMO=true)
"""

import os
import sys
import logging
import requests

from sync import mystore_get_all_products, po_get_all_products, po_headers, PO_BASE_URL

log = logging.getLogger("produkter_til_po")

PRODUKT_SALGSKONTO = int(os.environ.get("PRODUKT_SALGSKONTO", "3000"))
ORDRE_ENABLED = os.environ.get("ORDRE_ENABLED", "false").lower() == "true"
KUN_DEMO = os.environ.get("KUN_DEMO", "true").lower() == "true"


def main(skriv: bool):
    if skriv:
        if not ORDRE_ENABLED:
            log.error("ORDRE_ENABLED er ikke true - ingenting opprettes.")
            sys.exit(1)
        if KUN_DEMO and "/demo/" not in PO_BASE_URL.lower():
            log.error("KUN_DEMO er paa, men PO_BASE_URL er ikke demo (%s) - stopper.", PO_BASE_URL)
            sys.exit(1)

    ms = mystore_get_all_products()
    po = po_get_all_products()
    mangler = [p for p in ms if p["articleNumber"] and p["articleNumber"] not in po]
    oppsummering = f"Mystore: {len(ms)} varer | PowerOffice: {len(po)} varer | mangler i PO: {len(mangler)}"
    print(oppsummering + "\n")
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::notice::{oppsummering}")

    ok = feil = 0
    for p in mangler:
        linje = f"  {p['articleNumber']:<18} utpris {p['price']:>10,.2f}  innpris {p['purchasePrice']:>10,.2f}  {p['name'][:50]}"
        if not skriv:
            print(linje)
            continue
        payload = {
            "Code": p["articleNumber"],
            "Name": (p["name"] or p["articleNumber"])[:400],
            "ProductType": "Product",
            "StandardSalesAccount": PRODUKT_SALGSKONTO,
            "UnitPrice": p["price"],
            "UnitCost": p["purchasePrice"],
        }
        r = requests.post(f"{PO_BASE_URL}/Products", headers=po_headers(), json=payload, timeout=30)
        if r.status_code in (200, 201):
            ok += 1
            print(linje + "  -> opprettet")
        elif r.status_code == 400 and "is in use" in r.text:
            # Varen finnes allerede i PO (PO-listen kan vaere avkortet) - ikke en feil
            print(linje + "  -> finnes allerede")
        else:
            feil += 1
            print(linje + f"  -> FEIL {r.status_code}: {r.text[:200]}")

    print(f"\nFerdig: {ok} opprettet, {feil} feil." if skriv else "\nTORRKJORING - ingenting er opprettet.")
    if feil:
        sys.exit(1)


if __name__ == "__main__":
    try:
        main(skriv=(len(sys.argv) > 1 and sys.argv[1] == "kjor"))
    except Exception as e:
        resp = getattr(e, "response", None)
        detalj = f" | {resp.status_code} {resp.text[:300]}" if resp is not None else ""
        if os.environ.get("GITHUB_ACTIONS"):
            print(f"::error::{type(e).__name__}: {str(e)[:300]}{detalj}".replace("\n", " "))
        raise
