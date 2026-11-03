"""The apps (connectors/apps.py says how they're used). Each is written to the app's own published API."""
import base64
import re
import secrets
from datetime import datetime, timedelta
from urllib.parse import urlencode

import requests
from flask import current_app

from .apps import TIMEOUT, App, AppProblem, register

PAGE_LIMIT = 10  # pages of items fetched in one sync at most (the rest come next time)


def _gid(value):
    """Shopify's "gid://shopify/Order/123" as 123."""
    return int(str(value).rsplit("/", 1)[-1]) if value else None


@register
class Shopify(App):
    key, name, icon = "shopify", "Shopify", "shopping-cart"
    about = "Orders, customers and products from your Shopify store, and stock levels back to it."
    fields = [{"key": "store", "label": "Your store's address", "placeholder": "your-store.myshopify.com"},
              {"key": "token", "label": "Admin API access token", "placeholder": "shpat_…", "secret": True,
               "help": "In Shopify: Settings › Apps › Develop apps › Create an app, give it read access to orders, customers and "
                       "products (and write access to inventory to send stock back), install it, and copy its Admin API access token."}]
    pulls = {"orders": "Orders, with their lines", "customers": "Customers", "products": "Products (each variant, with its SKU and stock)"}
    item_details = {
        "orders": "id, number (#1001), created_at, email, customer.name, customer.email, total, currency, payment_status, "
                  "fulfilment_status, ship_to.name, ship_to.address (one line), ship_to.phone; lines: sku, title, quantity, price",
        "customers": "id, name, email, phone, orders (how many), spent, address",
        "products": "id, name (product and variant), sku, barcode, price, stock",
    }
    VERSION = "2025-01"

    def base(self, creds):
        store = re.sub(r"^https?://", "", creds["store"]).strip("/")
        return f"https://{store}/admin/api/{self.VERSION}"

    def headers(self, creds):
        return {"X-Shopify-Access-Token": creds["token"], "Content-Type": "application/json"}

    def graphql(self, creds, query, variables=None):
        answer = self.request(creds, "POST", "/graphql.json", json={"query": query, "variables": variables or {}})
        if answer.get("errors"):
            raise AppProblem(f"Shopify said: {answer['errors'][0].get('message') if isinstance(answer['errors'], list) else answer['errors']}")
        return answer.get("data") or {}

    def check(self, creds):
        if not re.fullmatch(r"(https?://)?[\w-]+\.myshopify\.com/?", creds["store"].strip()):
            raise AppProblem("Write the store's address as your-store.myshopify.com")
        return self.graphql(creds, "{ shop { name } }")["shop"]["name"]

    QUERIES = {
        "orders": ("orders", """id name createdAt updatedAt email displayFinancialStatus displayFulfillmentStatus
            currentTotalPriceSet { shopMoney { amount currencyCode } } customer { displayName email }
            shippingAddress { name address1 address2 city province zip country phone }
            lineItems(first: 100) { nodes { sku title quantity originalUnitPriceSet { shopMoney { amount } } } }"""),
        "customers": ("customers", "id displayName email phone updatedAt numberOfOrders amountSpent { amount } defaultAddress { formatted }"),
        "products": ("productVariants", "id sku barcode title price inventoryQuantity updatedAt product { title }"),
    }

    def pull(self, creds, what, since):
        name, fields = self.QUERIES[what]
        query = f"""query($q: String, $after: String) {{ {name}(first: 100, after: $after, query: $q, sortKey: UPDATED_AT) {{
            pageInfo {{ hasNextPage endCursor }} nodes {{ {fields} }} }} }}"""
        items, after, newest = [], None, since
        for _ in range(PAGE_LIMIT):
            page = self.graphql(creds, query, {"q": f"updated_at:>'{since}'" if since else None, "after": after})[name]
            for node in page["nodes"]:
                items.append(self._item(what, node))
                newest = max(newest or "", node.get("updatedAt") or "")
            if not page["pageInfo"]["hasNextPage"]:
                break
            after = page["pageInfo"]["endCursor"]
        return items, newest

    def _item(self, what, n):
        if what == "orders":
            ship = n.get("shippingAddress") or {}
            return {"id": _gid(n["id"]), "number": n["name"], "created_at": n.get("createdAt"), "email": n.get("email"),
                    "customer": {"name": (n.get("customer") or {}).get("displayName"), "email": (n.get("customer") or {}).get("email")},
                    "total": float(n["currentTotalPriceSet"]["shopMoney"]["amount"]), "currency": n["currentTotalPriceSet"]["shopMoney"]["currencyCode"],
                    "payment_status": n.get("displayFinancialStatus"), "fulfilment_status": n.get("displayFulfillmentStatus"),
                    "ship_to": {"name": ship.get("name"), "phone": ship.get("phone"),
                                "address": ", ".join(x for x in (ship.get("address1"), ship.get("address2"), ship.get("city"), ship.get("province"),
                                                                 ship.get("zip"), ship.get("country")) if x)},
                    "lines": [{"sku": li.get("sku"), "title": li.get("title"), "quantity": li.get("quantity"),
                               "price": float(li["originalUnitPriceSet"]["shopMoney"]["amount"])} for li in n["lineItems"]["nodes"]]}
        if what == "customers":
            return {"id": _gid(n["id"]), "name": n.get("displayName"), "email": n.get("email"), "phone": n.get("phone"),
                    "orders": int(n.get("numberOfOrders") or 0), "spent": float((n.get("amountSpent") or {}).get("amount") or 0),
                    "address": ", ".join((n.get("defaultAddress") or {}).get("formatted") or [])}
        title = n["product"]["title"] + ("" if n.get("title") in (None, "Default Title") else f" · {n['title']}")
        return {"id": _gid(n["id"]), "name": title, "sku": n.get("sku"), "barcode": n.get("barcode"), "price": float(n.get("price") or 0),
                "stock": n.get("inventoryQuantity")}


@register
class WooCommerce(App):
    key, name, icon = "woocommerce", "WooCommerce", "store"
    about = "Orders, customers and products from your WooCommerce shop."
    fields = [{"key": "url", "label": "Your shop's address", "placeholder": "https://shop.example.com"},
              {"key": "consumer_key", "label": "Consumer key", "placeholder": "ck_…", "secret": True,
               "help": "In WordPress: WooCommerce › Settings › Advanced › REST API › Add key, with Read/Write permission."},
              {"key": "consumer_secret", "label": "Consumer secret", "placeholder": "cs_…", "secret": True}]
    pulls = {"orders": "Orders, with their lines", "customers": "Customers", "products": "Products"}
    item_details = {
        "orders": "id, number, created_at, status, total, currency, customer.name, customer.email, customer.phone, ship_to.name, "
                  "ship_to.address; lines: sku, title, quantity, price",
        "customers": "id, name, email, phone, address",
        "products": "id, name, sku, price, stock, status",
    }

    def base(self, creds):
        return f"{creds['url'].rstrip('/')}/wp-json/wc/v3"

    def auth(self, creds):
        return (creds["consumer_key"], creds["consumer_secret"])

    def check(self, creds):
        if not creds["url"].startswith("https://"):
            raise AppProblem("The shop's address must start with https:// (its keys are only sent over a secure connection)")
        status = self.request(creds, "GET", "/system_status")
        return (status.get("environment") or {}).get("site_url") or creds["url"]

    def pull(self, creds, what, since):
        items, newest = [], since
        for page in range(1, PAGE_LIMIT + 1):
            params = {"per_page": 100, "page": page, "orderby": "modified" if what != "customers" else "id", "order": "asc"}
            if since and what != "customers":
                params["modified_after"] = since
            batch = self.request(creds, "GET", f"/{what}", params=params)
            for n in batch:
                if what == "customers" and since and int(n["id"]) <= int(since):
                    continue
                items.append(self._item(what, n))
                newest = str(n["id"]) if what == "customers" else max(newest or "", n.get("date_modified_gmt") or "")
            if len(batch) < 100:
                break
        return items, newest

    @staticmethod
    def _address(a):
        return ", ".join(x for x in (a.get("address_1"), a.get("address_2"), a.get("city"), a.get("state"), a.get("postcode"), a.get("country")) if x)

    def _item(self, what, n):
        if what == "orders":
            b, s = n.get("billing") or {}, n.get("shipping") or {}
            return {"id": n["id"], "number": n.get("number"), "created_at": n.get("date_created_gmt"), "status": n.get("status"),
                    "total": float(n.get("total") or 0), "currency": n.get("currency"),
                    "customer": {"name": f"{b.get('first_name', '')} {b.get('last_name', '')}".strip(), "email": b.get("email"), "phone": b.get("phone")},
                    "ship_to": {"name": f"{s.get('first_name', '')} {s.get('last_name', '')}".strip(), "address": self._address(s)},
                    "lines": [{"sku": li.get("sku"), "title": li.get("name"), "quantity": li.get("quantity"), "price": float(li.get("price") or 0)}
                              for li in n.get("line_items") or []]}
        if what == "customers":
            return {"id": n["id"], "name": f"{n.get('first_name', '')} {n.get('last_name', '')}".strip() or n.get("username"),
                    "email": n.get("email"), "phone": (n.get("billing") or {}).get("phone"), "address": self._address(n.get("billing") or {})}
        return {"id": n["id"], "name": n.get("name"), "sku": n.get("sku"), "price": float(n.get("price") or 0),
                "stock": n.get("stock_quantity"), "status": n.get("status")}


@register
class Stripe(App):
    key, name, icon = "stripe", "Stripe", "credit-card"
    about = "Card payments: a payment link on each invoice, and the invoice marked paid when the customer pays."
    fields = [{"key": "secret_key", "label": "Secret key", "placeholder": "sk_live_… or rk_live_…", "secret": True,
               "help": "In Stripe: Developers › API keys. A restricted key needs Checkout Sessions and Payment Intents."},
              {"key": "webhook_secret", "label": "Webhook signing secret", "placeholder": "whsec_…", "secret": True,
               "help": "In Stripe: Developers › Webhooks › Add endpoint, with the address shown here after connecting, for the event "
                       "checkout.session.completed; then copy its signing secret."}]
    pulls = {"payments": "Payments received"}
    item_details = {"payments": "id, amount, currency, status, email, description, created_at"}

    def base(self, creds):
        return "https://api.stripe.com/v1"

    def headers(self, creds):
        return {"Authorization": f"Bearer {creds['secret_key']}"}

    def check(self, creds):
        account = self.request(creds, "GET", "/account")
        return (account.get("business_profile") or {}).get("name") or (account.get("settings") or {}).get("dashboard", {}).get("display_name") \
            or account.get("email") or "your Stripe account"

    def pull(self, creds, what, since):
        params = {"limit": 100, **({"created[gt]": since} if since else {})}
        found = self.request(creds, "GET", "/payment_intents", params=params).get("data") or []
        items = [{"id": p["id"], "amount": (p.get("amount_received") or p.get("amount") or 0) / 100, "currency": (p.get("currency") or "").upper(),
                  "status": p.get("status"), "email": p.get("receipt_email"), "description": p.get("description"),
                  "created_at": datetime.utcfromtimestamp(p["created"]).isoformat()} for p in found]
        return items, str(max([p["created"] for p in found], default=int(since or 0))) if found or since else None

    def checkout(self, creds, amount, currency, description, success_url, metadata):
        """A Checkout page for the amount: its address (Stripe takes form fields, not JSON)."""
        data = {"mode": "payment", "success_url": success_url, "line_items[0][quantity]": 1,
                "line_items[0][price_data][currency]": currency.lower(), "line_items[0][price_data][unit_amount]": int(round(float(amount) * 100)),
                "line_items[0][price_data][product_data][name]": description[:200]}
        data.update({f"metadata[{k}]": str(v)[:500] for k, v in metadata.items()})
        session = self.request(creds, "POST", "/checkout/sessions", data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
        return session["url"], session["id"]


@register
class EasyPost(App):
    key, name, icon = "easypost", "EasyPost (shipping labels)", "truck"
    about = "Shipping labels and tracking numbers from your carrier accounts (Australia Post, Royal Mail, UPS, FedEx, DHL, USPS…)."
    fields = [{"key": "api_key", "label": "API key", "placeholder": "EZAK…", "secret": True,
               "help": "In EasyPost: Account Settings › API Keys. Add your carrier accounts there too."}]
    pulls = {}

    def base(self, creds):
        return "https://api.easypost.com/v2"

    def auth(self, creds):
        return (creds["api_key"], "")

    def check(self, creds):
        accounts = self.request(creds, "GET", "/carrier_accounts")
        names = sorted({a.get("readable") or a.get("type") for a in accounts} if isinstance(accounts, list) else set())
        return f"EasyPost ({', '.join(names[:4]) or 'no carriers added yet'})"


class _OAuthApp(App):
    """An app signed in to (its client id and secret are the platform's own, in .env)."""
    oauth = True
    fields = []
    authorize_url = token_url = scope = ""
    env = ""

    def client(self):
        cid, secret = current_app.config.get(f"{self.env}_CLIENT_ID"), current_app.config.get(f"{self.env}_CLIENT_SECRET")
        if not cid or not secret:
            raise AppProblem(f"Signing in to {self.name} needs the platform's {self.name} app first: {self.env}_CLIENT_ID and "
                             f"{self.env}_CLIENT_SECRET in .env")
        return cid, secret

    def sign_in_url(self, redirect_uri, state):
        cid, _ = self.client()
        return f"{self.authorize_url}?{urlencode({'response_type': 'code', 'client_id': cid, 'redirect_uri': redirect_uri, 'scope': self.scope, 'state': state})}"

    def _token(self, form):
        cid, secret = self.client()
        basic = base64.b64encode(f"{cid}:{secret}".encode()).decode()
        try:
            answer = requests.post(self.token_url, data=form, headers={"Authorization": f"Basic {basic}", "Accept": "application/json"}, timeout=TIMEOUT)
        except requests.RequestException as exc:
            raise AppProblem(f"{self.name} couldn't be reached to sign in ({type(exc).__name__})") from exc
        body = answer.json() if answer.headers.get("content-type", "").startswith("application/json") else {}
        if answer.status_code >= 400 or not body.get("access_token"):
            raise AppProblem(f"{self.name} didn't sign in: {body.get('error_description') or body.get('error') or answer.status_code}")
        return {"access_token": body["access_token"], "refresh_token": body.get("refresh_token"),
                "expires_at": (datetime.utcnow() + timedelta(seconds=int(body.get("expires_in") or 1800))).isoformat()}

    def finish(self, code, redirect_uri, args):
        return {**self._token({"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri}), **self.after_sign_in(args)}

    def refresh(self, details):
        renewed = self._token({"grant_type": "refresh_token", "refresh_token": details["refresh_token"]})
        return {**details, **{k: v for k, v in renewed.items() if v}}

    def after_sign_in(self, args):
        return {}

    def headers(self, creds):
        return {"Authorization": f"Bearer {creds['access_token']}", "Accept": "application/json"}


@register
class Xero(_OAuthApp):
    key, name, icon, env = "xero", "Xero", "landmark", "XERO"
    about = "Your books: invoices, bills and contacts sent to Xero, and payments brought back."
    authorize_url = "https://login.xero.com/identity/connect/authorize"
    token_url = "https://identity.xero.com/connect/token"
    scope = "openid profile email offline_access accounting.transactions accounting.contacts accounting.settings.read"
    pulls = {"invoices": "Invoices (sales and bills) and what's paid on them", "contacts": "Contacts", "payments": "Payments"}
    item_details = {"invoices": "id, number, type (ACCREC sales, ACCPAY bills), contact, date, due_date, status, total, paid, due, currency",
                    "contacts": "id, name, email, phone, is_customer, is_supplier",
                    "payments": "id, invoice_number, amount, date, reference"}

    def base(self, creds):
        return "https://api.xero.com/api.xro/2.0"

    def headers(self, creds):
        return {**super().headers(creds), "Xero-tenant-id": creds.get("tenant_id", "")}

    def finish(self, code, redirect_uri, args):
        details = self._token({"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri})
        try:
            tenants = requests.get("https://api.xero.com/connections", headers={"Authorization": f"Bearer {details['access_token']}"}, timeout=TIMEOUT).json()
        except (requests.RequestException, ValueError) as exc:
            raise AppProblem("Xero didn't say which organisation was chosen. Try again.") from exc
        if not tenants:
            raise AppProblem("No Xero organisation was chosen. Try again and pick one.")
        return {**details, "tenant_id": tenants[0]["tenantId"], "account": tenants[0].get("tenantName")}

    def check(self, creds):
        return creds.get("account") or "Xero"

    def pull(self, creds, what, since):
        path = {"invoices": "/Invoices", "contacts": "/Contacts", "payments": "/Payments"}[what]
        headers = {"If-Modified-Since": since} if since else {}
        items, started = [], datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
        for page in range(1, PAGE_LIMIT + 1):
            got = self.request(creds, "GET", path, params={"page": page} if what != "payments" else None, headers=headers)
            batch = got.get(path.strip("/")) or []
            items += [self._item(what, n) for n in batch]
            if what == "payments" or len(batch) < 100:
                break
        return items, started

    @staticmethod
    def _date(value):
        m = re.search(r"/Date\((\d+)", str(value or ""))
        return datetime.utcfromtimestamp(int(m.group(1)) / 1000).date().isoformat() if m else value

    def _item(self, what, n):
        if what == "invoices":
            return {"id": n["InvoiceID"], "number": n.get("InvoiceNumber"), "type": n.get("Type"), "contact": (n.get("Contact") or {}).get("Name"),
                    "date": self._date(n.get("Date")), "due_date": self._date(n.get("DueDate")), "status": n.get("Status"),
                    "total": n.get("Total"), "paid": n.get("AmountPaid"), "due": n.get("AmountDue"), "currency": n.get("CurrencyCode")}
        if what == "contacts":
            return {"id": n["ContactID"], "name": n.get("Name"), "email": n.get("EmailAddress"),
                    "phone": next((p.get("PhoneNumber") for p in n.get("Phones") or [] if p.get("PhoneNumber")), None),
                    "is_customer": n.get("IsCustomer"), "is_supplier": n.get("IsSupplier")}
        return {"id": n["PaymentID"], "invoice_number": (n.get("Invoice") or {}).get("InvoiceNumber"), "amount": n.get("Amount"),
                "date": self._date(n.get("Date")), "reference": n.get("Reference")}


@register
class QuickBooks(_OAuthApp):
    key, name, icon, env = "quickbooks", "QuickBooks Online", "landmark", "QBO"
    about = "Your books: invoices, customers and bills sent to QuickBooks, and payments brought back."
    authorize_url = "https://appcenter.intuit.com/connect/oauth2"
    token_url = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
    scope = "com.intuit.quickbooks.accounting"
    pulls = {"invoices": "Invoices and what's owed on them", "customers": "Customers", "payments": "Payments received"}
    item_details = {"invoices": "id, number, customer, date, due_date, total, balance (still owed)",
                    "customers": "id, name, email, phone, balance", "payments": "id, customer, amount, date"}
    ENTITIES = {"invoices": "Invoice", "customers": "Customer", "payments": "Payment"}

    def base(self, creds):
        host = "sandbox-quickbooks.api.intuit.com" if current_app.config.get("QBO_SANDBOX") else "quickbooks.api.intuit.com"
        return f"https://{host}/v3/company/{creds.get('realm_id', '')}"

    def after_sign_in(self, args):
        if not args.get("realmId"):
            raise AppProblem("QuickBooks didn't say which company was chosen. Try again.")
        return {"realm_id": args["realmId"]}

    def check(self, creds):
        info = self.request(creds, "GET", f"/companyinfo/{creds['realm_id']}")
        return (info.get("CompanyInfo") or {}).get("CompanyName") or "QuickBooks"

    def pull(self, creds, what, since):
        entity, started = self.ENTITIES[what], datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S-00:00")
        where = f" WHERE Metadata.LastUpdatedTime > '{since}'" if since else ""
        found = self.request(creds, "GET", "/query", params={"query": f"SELECT * FROM {entity}{where} STARTPOSITION 1 MAXRESULTS 1000"})
        rows = (found.get("QueryResponse") or {}).get(entity) or []
        return [self._item(what, n) for n in rows], started

    def _item(self, what, n):
        if what == "invoices":
            return {"id": n["Id"], "number": n.get("DocNumber"), "customer": (n.get("CustomerRef") or {}).get("name"), "date": n.get("TxnDate"),
                    "due_date": n.get("DueDate"), "total": n.get("TotalAmt"), "balance": n.get("Balance")}
        if what == "customers":
            return {"id": n["Id"], "name": n.get("DisplayName"), "email": (n.get("PrimaryEmailAddr") or {}).get("Address"),
                    "phone": (n.get("PrimaryPhone") or {}).get("FreeFormNumber"), "balance": n.get("Balance")}
        return {"id": n["Id"], "customer": (n.get("CustomerRef") or {}).get("name"), "amount": n.get("TotalAmt"), "date": n.get("TxnDate")}


def new_state():
    return secrets.token_urlsafe(24)
