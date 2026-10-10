"""
Fortloepende (nattlig) lagerregulering, konto 1460, MyStore -> PowerOffice.

Egen fil: endrer IKKE den eksisterende produktsynken (sync.py / sync.yml).
Den gjenbruker bare lesefunksjonene derfra.

  python lagerbilag.py lagerverdi [AAAA-MM-DD]  -> KUN LESING, viser tallene (trygt)
  python lagerbilag.py lagerbilag [AAAA-MM-DD]  -> posterer bilag (krever LAGERBILAG_ENABLED=true)
  Uten dato brukes gaarsdagen (norsk tid) - jobben kjoeres natt til hver dag.

Prinsipp:
  Fasit lagerverdi = sum(fysisk antall x innpris) i MyStore (naa, om natten)
  Bokfoert verdi   = saldo konto 1460 i PowerOffice til og med bilagsdatoen
  Differansen posteres mot beholdningsendring (4390).

Innkjoepsfakturaer foeres paa 4300 i PowerOffice; varemottak gjoeres i MyStore.
Reguleringen flytter det som ligger paa lager fra kostnad til balanse, og
tilbake naar varene selges. Fordi vi sammenligner mot faktisk saldo, retter den
seg selv: en feil eller en hoppet over natt blir riktig neste natt.

Sikringer:
  - Bilaget posteres direkte (Vouchers/ManualJournals) slik at saldoen er
    oppdatert til neste kjoering. Feil rettes med reversering i PowerOffice.
  - Én referanse per dag (ABC-LAGER-AAAA-MM-DD) hindrer dobbeltbilag.
  - Er perioden sperret i PowerOffice (sperredato), posteres ingenting.
  - Endringer under LAGER_MIN_DIFF kroner posteres ikke.
"""

import os
import sys
import json
import logging
import requests
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

# Kun lesefunksjoner fra den eksisterende synken - ingenting der endres eller kjoeres.
from sync import mystore_get_all_products, po_headers, PO_BASE_URL

log = logging.getLogger("lagerbilag")

LAGER_KONTO    = int(os.environ.get("LAGER_KONTO", "1460"))
LAGER_MOTKONTO = int(os.environ.get("LAGER_MOTKONTO", "4390"))  # Beholdningsendring varer for videresalg
LAGER_MIN_DIFF = float(os.environ.get("LAGER_MIN_DIFF", "1"))
LAGERBILAG_ENABLED = os.environ.get("LAGERBILAG_ENABLED", "false").lower() == "true"


def _po_get(url: str, params: dict | None = None):
    resp = requests.get(url, headers=po_headers(), params=params, timeout=60)
    if resp.status_code == 204 or not resp.text.strip():
        return None, resp
    if resp.status_code != 200:
        log.error("PowerOffice GET %s feil %s: %s", url, resp.status_code, resp.text[:400])
        resp.raise_for_status()
    return resp.json(), resp


def _po_get_paged(url: str, params: dict | None = None) -> list:
    rows, first = [], True
    while url:
        data, resp = _po_get(url, params if first else None)
        first = False
        if data is None:
            break
        rows += data if isinstance(data, list) else (data.get("data") or data.get("value") or [])
        url = None
        pag = resp.headers.get("x-pagination")
        if pag:
            try:
                p = json.loads(pag)
                url = p.get("nextPageLink") or p.get("NextPageLink")
            except ValueError:
                pass
    return rows


def bilagsdato(arg: str | None) -> str:
    if arg:
        return date.fromisoformat(arg).isoformat()
    return (datetime.now(ZoneInfo("Europe/Oslo")).date() - timedelta(days=1)).isoformat()


def beregn_lagerverdi() -> tuple[float, list, list]:
    produkter = mystore_get_all_products()
    med_lager = [p for p in produkter if p["stockQuantity"] > 0]
    uten_innpris = [p for p in med_lager if p["purchasePrice"] <= 0]
    gyldige = [p for p in med_lager if p["purchasePrice"] > 0]
    total = round(sum(p["stockQuantity"] * p["purchasePrice"] for p in gyldige), 2)
    topp = sorted(gyldige, key=lambda p: p["stockQuantity"] * p["purchasePrice"], reverse=True)[:10]
    return total, uten_innpris, topp


def po_kontosaldo(kontonr: int, til_dato: str) -> float:
    trans = _po_get_paged(f"{PO_BASE_URL}/AccountTransactions", {
        "fromDate": "2000-01-01", "toDate": til_dato, "accountNos": str(kontonr),
    })
    return round(sum(float(t.get("Amount") or 0) for t in trans), 2)


def po_konto_id(kontonr: int) -> int:
    for k in _po_get_paged(f"{PO_BASE_URL}/GeneralLedgerAccounts", {"accountNos": str(kontonr)}):
        if str(k.get("AccountNo")) == str(kontonr):
            return int(k["Id"])
    raise RuntimeError(f"Fant ikke konto {kontonr} i PowerOffice")


def po_mva_fri_id() -> int:
    """Id for mva-kode 0 (ingen mva-behandling) - paakrevd paa hver bilagslinje."""
    for v in _po_get_paged(f"{PO_BASE_URL}/VatCodes"):
        if str(v.get("Code")) == "0":
            return int(v["Id"])
    raise RuntimeError("Fant ikke mva-kode 0 i PowerOffice")


def po_sperredato() -> str | None:
    data, _ = _po_get(f"{PO_BASE_URL}/LockDateSettings")
    if isinstance(data, list):
        data = data[0] if data else None
    return (data or {}).get("LockDate", None) and str(data["LockDate"])[:10]


TILGANGER_BEHOV = {
    "Bilag (postering)":       ("voucher",),
    "Kontotransaksjoner":      ("accounttransaction",),
    "Hovedbokskontoer":        ("generalledger", "accountingsetting"),
    "Mva-koder":               ("vatcode", "accountingsetting", "commonservices", "generalledger"),
    "Sperredato":              ("lockdate", "accountingsetting", "commonservices", "generalledger"),
}


def tilgangssjekk() -> bool:
    """Leser hvilke API-tilganger integrasjonen har (kun lesing)."""
    data, _ = _po_get(f"{PO_BASE_URL}/ClientIntegrationInformation")
    data = (data[0] if isinstance(data, list) and data else data) or {}
    gyldige = [str(p) for p in (data.get("ValidPrivileges") or [])]
    ugyldige = [str(p) for p in (data.get("InvalidPrivileges") or [])]
    print(f"\nKlient: {data.get('ClientName')}")
    print(f"Gyldige tilganger:  {', '.join(gyldige) or '-'}")
    print(f"Ugyldige tilganger: {', '.join(ugyldige) or '-'}")
    print("\nTilgangssjekk for lagerregulering:")
    alt_ok = True
    for navn, noekler in TILGANGER_BEHOV.items():
        treff = [p for p in gyldige if any(k in p.lower() for k in noekler)]
        ok = bool(treff)
        alt_ok &= ok
        print(f"  [{'OK' if ok else 'MANGLER'}] {navn:<22} {', '.join(treff)}")
    return alt_ok


def lagerbilag(dato_arg: str | None, send: bool):
    dato = bilagsdato(dato_arg)
    log.info("=== Lagerregulering per %s ===", dato)
    try:
        tilgangssjekk()
    except Exception as e:  # sjekken skal aldri stoppe selve tallene
        log.warning("Kunne ikke lese tilganger: %s", e)

    fasit, uten_innpris, topp = beregn_lagerverdi()
    bokfort = po_kontosaldo(LAGER_KONTO, dato)
    diff = round(fasit - bokfort, 2)

    print(f"\nLagerverdi MyStore (antall x innpris): {fasit:>14,.2f} kr")
    print(f"Saldo konto {LAGER_KONTO} i PowerOffice:    {bokfort:>14,.2f} kr")
    print(f"Differanse som reguleres:            {diff:>14,.2f} kr\n")

    print("Ti stoerste lagerverdier (kontroll):")
    for p in topp:
        print(f"  {p['articleNumber']:<16} {p['stockQuantity']:>5} x {p['purchasePrice']:>10,.2f}"
              f" = {p['stockQuantity'] * p['purchasePrice']:>12,.2f}  {p['name'][:50]}")
    if uten_innpris:
        print(f"\nOBS: {len(uten_innpris)} varer har lager men innpris 0 - telles ikke med:")
        for p in uten_innpris[:25]:
            print(f"  {p['articleNumber']:<16} antall {p['stockQuantity']:>5}  {p['name'][:60]}")

    if abs(diff) < LAGER_MIN_DIFF:
        log.info("Differanse under %.0f kr - ingen bilag.", LAGER_MIN_DIFF)
        return

    tekst = f"Lagerregulering {dato} (MyStore-lagerverdi {fasit:,.2f})"
    retning = f"debet {LAGER_KONTO} / kredit {LAGER_MOTKONTO}" if diff > 0 \
        else f"debet {LAGER_MOTKONTO} / kredit {LAGER_KONTO}"
    print(f"\nBilag {dato}: {retning}  {abs(diff):,.2f} kr")

    if not send:
        print("\nTORRKJORING - ingenting er sendt til PowerOffice.")
        return
    if not LAGERBILAG_ENABLED:
        log.error("LAGERBILAG_ENABLED er ikke true - bilag sendes ikke.")
        sys.exit(1)

    sperre = po_sperredato()
    if sperre and dato <= sperre:
        log.warning("Perioden er sperret til %s i PowerOffice - ingen bilag posteres.", sperre)
        return

    mva0 = po_mva_fri_id()
    linje = lambda konto, beloep: {
        "AccountId": po_konto_id(konto), "CurrencyAmount": beloep, "VatId": mva0,
        "PostingDate": dato, "Description": tekst,
    }
    payload = {
        "CurrencyCode": "NOK",
        "VoucherDate": dato,
        "Description": tekst,
        "ExternalImportReference": f"ABC-LAGER-{dato}",
        "VoucherLines": [linje(LAGER_KONTO, diff), linje(LAGER_MOTKONTO, -diff)],
    }
    resp = requests.post(f"{PO_BASE_URL}/Vouchers/ManualJournals",
                         headers=po_headers(), json=payload, timeout=60)
    if resp.status_code in (200, 201):
        log.info("Bilag postert: %s", resp.text[:300])
    else:
        log.error("Bilag feilet %s: %s", resp.status_code, resp.text[:600])
        sys.exit(1)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "lagerverdi"
    dato_arg = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] else None
    lagerbilag(dato_arg, send=(mode == "lagerbilag"))
