"""
MyStore -> PowerOffice produktsynk
Synkroniserer antall (lager), innpris og utpris for alle produkter.

Kjores automatisk via GitHub Actions (se .github/workflows/sync.yml).
Kan ogsa kjores manuelt: python sync.py

Nodvendige miljovariabler (legg inn som GitHub Secrets):
  MYSTORE_TOKEN       - Personal Access Token fra MyStore admin
  MYSTORE_SHOP        - Butikknavnet ditt (f.eks. abcfallsikring)
  PO_APP_KEY          - Application Key fra PowerOffice developer-portal
  PO_CLIENT_KEY       - Client Key fra PowerOffice
  PO_SUBSCRIPTION_KEY - Subscription Key fra PowerOffice developer-portal
"""

import os
import sys
import base64
import logging
import requests
from datetime import datetime

logging.basicConfig(level=logging.INFO, format='%(asctime)s  %(levelname)-8s  %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
log = logging.getLogger(__name__)

MYSTORE_API_KEY     = os.environ['MYSTORE_TOKEN']
MYSTORE_STORE_NAME  = os.environ['MYSTORE_SHOP']
MYSTORE_BASE_URL    = 'https://api.mystore.no/v1'

PO_APP_KEY          = os.environ['PO_APP_KEY']
PO_CLIENT_KEY       = os.environ['PO_CLIENT_KEY']
PO_SUBSCRIPTION_KEY = os.environ['PO_SUBSCRIPTION_KEY']
PO_BASE_URL         = 'https://goapi.poweroffice.net/v2'
PO_TOKEN_URL        = 'https://goapi.poweroffice.net/OAuth/Token'


def mystore_get_all_products():
    headers = {'Authorization': f'Bearer {MYSTORE_API_KEY}', 'Accept': 'application/json', 'X-Store': MYSTORE_STORE_NAME}
    products = []
    page = 1
    while True:
        resp = requests.get(f'{MYSTORE_BASE_URL}/products', headers=headers,
            params={'page': page, 'pageSize': 100, 'includeStock': 'true', 'includePrice': 'true'}, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        batch = data.get('products') or data.get('data') or data
        if not batch:
            break
        for p in batch:
            products.append({
                'articleNumber': p.get('articleNumber') or p.get('sku') or p.get('id'),
                'name':          p.get('name') or p.get('productName', ''),
                'price':         float(p.get('price') or p.get('salesPrice') or 0),
                'purchasePrice': float(p.get('purchasePrice') or p.get('costPrice') or 0),
                'stockQuantity': int(p.get('physicalStock') or p.get('stockQuantity') or p.get('stock') or 0),
            })
        total = data.get('total') or data.get('totalCount') or len(batch)
        if page * 100 >= total:
            break
        page += 1
    log.info('MyStore: hentet %d produkter', len(products))
    return products


_po_token = None
_po_token_expires = None


def po_get_token():
    global _po_token, _po_token_expires
    if _po_token and _po_token_expires and datetime.utcnow() < _po_token_expires:
        return _po_token
    credentials = base64.b64encode(f'{PO_APP_KEY}:{PO_CLIENT_KEY}'.encode()).decode()
    resp = requests.post(PO_TOKEN_URL, headers={
        'Authorization': f'Basic {credentials}',
        'Ocp-Apim-Subscription-Key': PO_SUBSCRIPTION_KEY,
        'Content-Type': 'application/x-www-form-urlencoded',
    }, data='grant_type=client_credentials', timeout=15)
    resp.raise_for_status()
    token_data = resp.json()
    _po_token = token_data['access_token']
    _po_token_expires = datetime.utcnow()
    log.info('PowerOffice: nytt access token hentet')
    return _po_token


def po_headers():
    return {'Authorization': f'Bearer {po_get_token()}', 'Ocp-Apim-Subscription-Key': PO_SUBSCRIPTION_KEY,
            'Content-Type': 'application/json', 'Accept': 'application/json'}


def po_get_all_products():
    products = {}
    skip = 0
    while True:
        resp = requests.get(f'{PO_BASE_URL}/products', headers=po_headers(),
            params={'$top': 100, '$skip': skip}, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        batch = data.get('data') or (data if isinstance(data, list) else [])
        if not batch:
            break
        for p in batch:
            code = p.get('code') or p.get('articleNumber') or p.get('productCode')
            if code:
                products[str(code)] = p
        if len(batch) < 100:
            break
        skip += 100
    log.info('PowerOffice: hentet %d produkter', len(products))
    return products


def po_update_product(po_id, payload):
    resp = requests.patch(f'{PO_BASE_URL}/products/{po_id}', headers=po_headers(), json=payload, timeout=15)
    if resp.status_code in (200, 204):
        return True
    log.warning('PO PATCH %s feil %s: %s', po_id, resp.status_code, resp.text[:200])
    return False


def po_set_stock(po_id, quantity):
    resp = requests.post(f'{PO_BASE_URL}/products/{po_id}/stockEntries', headers=po_headers(),
        json={'quantity': quantity, 'entryType': 'ManualAdjustment'}, timeout=15)
    if resp.status_code in (200, 201, 204):
        return True
    log.warning('PO stock %s feil %s: %s', po_id, resp.status_code, resp.text[:200])
    return False


def run_sync():
    log.info('=== Starter MyStore -> PowerOffice synk ===')
    mystore_products = mystore_get_all_products()
    po_products = po_get_all_products()
    updated = not_found = errors = 0
    for ms in mystore_products:
        article_no = str(ms['articleNumber'])
        po = po_products.get(article_no)
        if not po:
            not_found += 1
            continue
        po_id = str(po.get('id') or po.get('productId'))
        price_payload = {}
        if ms['price'] > 0:
            price_payload['salesPrice'] = ms['price']
        if ms['purchasePrice'] > 0:
            price_payload['purchasePrice'] = ms['purchasePrice']
        price_ok = po_update_product(po_id, price_payload) if price_payload else True
        stock_ok = po_set_stock(po_id, ms['stockQuantity'])
        if price_ok and stock_ok:
            log.info('OK  %s  antall=%d  inn=%.2f  ut=%.2f', article_no, ms['stockQuantity'], ms['purchasePrice'], ms['price'])
            updated += 1
        else:
            errors += 1
    log.info('=== Ferdig: %d oppdatert, %d ikke funnet, %d feil ===', updated, not_found, errors)
    if errors > 0:
        sys.exit(1)


def test_mode():
    import json
    log.info('--- TEST MyStore ---')
    for p in mystore_get_all_products()[:3]:
        print(json.dumps(p, indent=2, ensure_ascii=False))
    log.info('--- TEST PowerOffice ---')
    for k, v in list(po_get_all_products().items())[:3]:
        print(json.dumps(v, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'test':
        test_mode()
    else:
        run_sync()
