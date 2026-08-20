"""Thin wrapper around the bits of the Xero REST API we use.

We deliberately stay on `requests` rather than the full `xero-python` SDK —
the surface area we need is small (auth + create one invoice + list one set
of connections) and the SDK pulls a lot of model code we'd never touch.
"""

from __future__ import annotations

from datetime import timedelta
from urllib.parse import urlencode

import requests
from django.conf import settings
from django.utils import timezone


AUTHORIZE_URL = "https://login.xero.com/identity/connect/authorize"
TOKEN_URL = "https://identity.xero.com/connect/token"
CONNECTIONS_URL = "https://api.xero.com/connections"
API_BASE = "https://api.xero.com/api.xro/2.0"


# ---- OAuth ----------------------------------------------------------------


def get_authorize_url(redirect_uri: str, state: str) -> str:
    params = {
        "response_type": "code",
        "client_id": settings.XERO_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "scope": settings.XERO_SCOPES,
        "state": state,
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def exchange_code_for_tokens(code: str, redirect_uri: str) -> dict:
    """Swap the auth code from Xero's callback for an access + refresh token pair."""
    r = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        },
        auth=(settings.XERO_CLIENT_ID, settings.XERO_CLIENT_SECRET),
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def list_authorised_tenants(access_token: str) -> list[dict]:
    """Each Xero user can authorise more than one org — this is which ones the
    token is good for. We take the first by default."""
    r = requests.get(
        CONNECTIONS_URL,
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def refresh_tokens(connection) -> None:
    """In-place: swap the refresh token for a fresh access + refresh pair."""
    r = requests.post(
        TOKEN_URL,
        data={"grant_type": "refresh_token", "refresh_token": connection.refresh_token},
        auth=(settings.XERO_CLIENT_ID, settings.XERO_CLIENT_SECRET),
        timeout=15,
    )
    r.raise_for_status()
    payload = r.json()
    connection.access_token = payload["access_token"]
    connection.refresh_token = payload["refresh_token"]
    connection.expires_at = timezone.now() + timedelta(seconds=int(payload.get("expires_in", 1800)))
    connection.scopes = payload.get("scope", connection.scopes)
    connection.save(update_fields=["access_token", "refresh_token", "expires_at", "scopes"])


def get_active_connection():
    """Return the connected Xero org with a fresh access token, or None if
    Xero isn't connected yet. Refreshes lazily when the token's about to expire."""
    from .models import XeroConnection

    conn = XeroConnection.objects.first()
    if conn is None:
        return None
    # Refresh ~30s before actual expiry so the next API call doesn't race the clock.
    if conn.expires_at <= timezone.now() + timedelta(seconds=30):
        refresh_tokens(conn)
    return conn


def is_configured() -> bool:
    """Whether the developer credentials are set. The Connect button needs both."""
    return bool(settings.XERO_CLIENT_ID and settings.XERO_CLIENT_SECRET)


# ---- Invoice creation -----------------------------------------------------


class XeroError(RuntimeError):
    """The Xero API rejected the request — message contains the validation detail."""


def _raise_on_error(r) -> None:
    """Surface Xero's error message rather than a generic 4xx."""
    if r.status_code < 400:
        return
    try:
        detail = r.json()
        msg = detail.get("Detail") or detail.get("Message") or r.text
    except Exception:
        msg = r.text
    raise XeroError(f"Xero said {r.status_code}: {msg}")


def create_invoice(
    *,
    contact_name: str,
    line_items: list[dict],
    reference: str = "",
    invoice_status: str = "DRAFT",
    due_days: int = 30,
) -> dict:
    """Create an ACCREC (you billing someone) invoice.

    `line_items` follow Xero's shape: `{Description, Quantity, UnitAmount,
    AccountCode, TaxType}`. The contact is matched by name — if it doesn't
    exist on the Xero org Xero creates a new one. Returns the created
    invoice dict (the first row of the API's Invoices list).
    """
    conn = get_active_connection()
    if conn is None:
        raise XeroError("Xero isn't connected.")

    today = timezone.now().date()
    payload = {
        "Invoices": [
            {
                "Type": "ACCREC",
                "Contact": {"Name": contact_name},
                "LineItems": line_items,
                "Date": today.isoformat(),
                "DueDate": (today + timedelta(days=due_days)).isoformat(),
                "Status": invoice_status,
                "Reference": reference or "",
            }
        ]
    }
    r = requests.post(
        f"{API_BASE}/Invoices",
        json=payload,
        headers={
            "Authorization": f"Bearer {conn.access_token}",
            "Xero-Tenant-Id": conn.tenant_id,
            "Accept": "application/json",
        },
        timeout=30,
    )
    _raise_on_error(r)
    body = r.json()
    invoices = body.get("Invoices") or []
    if not invoices:
        raise XeroError("Xero accepted the request but returned no invoice.")
    return invoices[0]


def online_invoice_url(xero_invoice_id: str) -> str:
    """Deep link to the invoice's edit page in the Xero web UI."""
    return f"https://go.xero.com/AccountsReceivable/Edit.aspx?InvoiceID={xero_invoice_id}"


# ---- Invoice sync ---------------------------------------------------------

# Xero caps a page at 100 invoices and the IDs filter travels in the query
# string, so chunk well below both limits.
_SYNC_CHUNK = 40


def list_invoices(invoice_ids: list[str]) -> list[dict]:
    """Fetch invoices by InvoiceID. Returns the raw invoice dicts."""
    conn = get_active_connection()
    if conn is None:
        raise XeroError("Xero isn't connected.")

    invoices: list[dict] = []
    for i in range(0, len(invoice_ids), _SYNC_CHUNK):
        chunk = invoice_ids[i:i + _SYNC_CHUNK]
        r = requests.get(
            f"{API_BASE}/Invoices",
            params={"IDs": ",".join(chunk)},
            headers={
                "Authorization": f"Bearer {conn.access_token}",
                "Xero-Tenant-Id": conn.tenant_id,
                "Accept": "application/json",
            },
            timeout=30,
        )
        _raise_on_error(r)
        invoices.extend(r.json().get("Invoices") or [])
    return invoices
