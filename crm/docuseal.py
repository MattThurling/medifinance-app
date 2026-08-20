"""Thin wrapper around the bits of the self-hosted DocuSeal API we use.

Templates are built by hand in the DocuSeal web UI; this module only creates
submissions against them (prefilled from deal data), tracks their state and
pulls down the signed PDFs. Auth is a static per-instance API token — no
OAuth, so unlike Xero there's no connection model to store.
"""

from __future__ import annotations

import requests
from django.conf import settings
from django.utils import timezone


class DocuSealError(RuntimeError):
    """The DocuSeal API rejected the request — message contains the detail."""


def is_configured() -> bool:
    """Whether this environment has a DocuSeal instance wired up. Without it,
    the send-for-signature UI stays hidden and the views short-circuit."""
    return bool(settings.DOCUSEAL_URL and settings.DOCUSEAL_API_TOKEN)


def _request(method: str, path: str, **kwargs) -> requests.Response:
    base = settings.DOCUSEAL_URL.rstrip("/") + "/api"
    r = requests.request(
        method,
        f"{base}{path}",
        headers={"X-Auth-Token": settings.DOCUSEAL_API_TOKEN},
        timeout=30,
        **kwargs,
    )
    if r.status_code >= 400:
        try:
            detail = r.json()
            msg = detail.get("error") or detail.get("message") or r.text
        except Exception:
            msg = r.text
        raise DocuSealError(f"DocuSeal said {r.status_code}: {msg}")
    return r


def list_templates() -> list[dict]:
    """All (non-archived) templates on the instance. One page is plenty at
    our volume; template ids differ between dev and prod so callers must
    always pick from this list rather than hardcode."""
    body = _request("GET", "/templates").json()
    # Paginated shape: {"data": [...], "pagination": {...}}
    return body.get("data", body if isinstance(body, list) else [])


def create_submission(
    *,
    template_id: int,
    signer_email: str,
    signer_name: str = "",
    values: dict | None = None,
    message: str | None = None,
    send_email: bool = True,
) -> dict:
    """Send a template to one signer, prefilled with `values` (keyed by the
    template's field names — unknown keys are ignored by DocuSeal).

    Returns {"submission_id": int, "submitter_id": int | None}.
    """
    submitter: dict = {"email": signer_email}
    if signer_name:
        submitter["name"] = signer_name
    if values:
        submitter["values"] = values
    payload: dict = {
        "template_id": template_id,
        "send_email": send_email,
        "submitters": [submitter],
    }
    if message:
        # A custom message replaces DocuSeal's whole email body — without the
        # {{submitter.link}} placeholder the email would have no signing link.
        if "{{submitter.link}}" not in message:
            message = f"{message}\n\n{{{{submitter.link}}}}"
        payload["message"] = {"body": message}
    body = _request("POST", "/submissions", json=payload).json()

    # Older DocuSeal versions return the submitters list directly; newer ones
    # return {"id": ..., "submitters": [...]}. Normalise to one shape.
    if isinstance(body, list):
        first = body[0] if body else {}
        return {"submission_id": first.get("submission_id"), "submitter_id": first.get("id")}
    submitters = body.get("submitters") or []
    return {
        "submission_id": body.get("id"),
        "submitter_id": submitters[0].get("id") if submitters else None,
    }


def get_submission(submission_id: int) -> dict:
    return _request("GET", f"/submissions/{submission_id}").json()


def get_submission_documents(submission_id: int) -> list[dict]:
    """The signed/combined PDFs for a submission: [{"name": ..., "url": ...}]."""
    body = _request("GET", f"/submissions/{submission_id}/documents").json()
    return body.get("documents", [])


def archive_submission(submission_id: int) -> None:
    """Archive (void) a submission — its signing links stop working."""
    _request("DELETE", f"/submissions/{submission_id}")


def download_file(url: str) -> bytes:
    """Fetch a document/audit-log file by the URL DocuSeal handed us."""
    r = requests.get(url, headers={"X-Auth-Token": settings.DOCUSEAL_API_TOKEN}, timeout=30)
    if r.status_code >= 400:
        raise DocuSealError(f"DocuSeal said {r.status_code} fetching {url}")
    return r.content


def build_prefill_values(deal) -> dict:
    """Field values every template can draw on. The convention: DocuSeal
    template fields must be named exactly like these keys ("Client Name",
    "Amount", …) to get prefilled; fields a template doesn't have are ignored.
    """
    customer = deal.customer
    values = {
        "Deal Name": deal.name,
        "Client Name": customer.full_name if customer else "",
        "Client Address": customer.one_line_address if customer else "",
        "Customer Email": customer.email if customer else "",
        "Organisation": deal.organisation.name if deal.organisation else "",
        "Date": timezone.now().date().strftime("%d/%m/%Y"),
    }
    if deal.finance_amount is not None:
        values["Amount"] = f"{deal.finance_amount:,.2f}"
    return values
