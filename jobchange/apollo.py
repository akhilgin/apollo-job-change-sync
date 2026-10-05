"""Apollo REST client (stdlib only) and queue normalization.

Auth: APOLLO_API_KEY env var, sent as the X-Api-Key header.
Nothing here runs unless a subcommand explicitly calls it.
"""
import csv
import json
import os
import time
import urllib.error
import urllib.request

from .rules import email_domain, norm_domain


class ApolloError(RuntimeError):
    pass


def _key():
    k = os.environ.get("APOLLO_API_KEY")
    if not k:
        raise ApolloError("APOLLO_API_KEY is not set")
    return k


def _post(cfg, path, body, retries=3):
    url = cfg["apollo"]["base_url"].rstrip("/") + path
    data = json.dumps(body).encode()
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, method="POST", headers={
            "Content-Type": "application/json", "Cache-Control": "no-cache", "X-Api-Key": _key()})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and attempt < retries - 1:
                time.sleep(2 ** (attempt + 1))
                continue
            raise ApolloError("HTTP {} from {}: {}".format(e.code, path, e.read().decode()[:300]))
    raise ApolloError("unreachable")


# ---------------------------------------------------------------------------
# UNVERIFIED: the job-change filter on contacts search.
# The parameter name below is the one part of this tool I could not confirm against
# Apollo's public docs. If `fetch --apollo` returns everything or nothing, check
# https://docs.apollo.io and fix this one function. Client-side filtering in
# normalize_apollo_contact() (processed/dismissed events) is applied either way.
# ---------------------------------------------------------------------------
def job_change_filter():
    return {"contact_job_changed": True}


def fetch_job_changes(cfg):
    """Page through contacts search with the job-change filter. Returns raw contact dicts."""
    out = []
    for page in range(1, cfg["apollo"]["max_pages"] + 1):
        body = {"page": page, "per_page": cfg["apollo"]["per_page"]}
        body.update(job_change_filter())
        resp = _post(cfg, "/contacts/search", body)
        out.extend(resp.get("contacts") or [])
        total = (resp.get("pagination") or {}).get("total_pages") or 1
        if page >= total:
            break
    return out


def bulk_match(cfg, details):
    """details: list of up to 10 dicts. Returns the list of matches, aligned to `details`
    (None where Apollo found nothing). Spends credits for revealed emails."""
    if len(details) > 10:
        raise ValueError("Apollo bulk match accepts at most 10 people per call")
    resp = _post(cfg, "/people/bulk_match?reveal_personal_emails=false", {"details": details})
    matches = resp.get("matches") or []
    if len(matches) != len(details):
        raise ApolloError("bulk_match returned {} results for {} inputs; refusing to guess alignment".format(
            len(matches), len(details)))
    return matches


def match_to_result(m):
    """Flatten an Apollo person object to the fields the accept rule needs."""
    if not m:
        return None
    org = m.get("organization") or {}
    return {
        "email": m.get("email") or "",
        "email_status": m.get("email_status") or "",
        "org_domain": org.get("primary_domain") or "",
        "first_name": m.get("first_name") or "",
        "last_name": m.get("last_name") or "",
        "title": m.get("title") or "",
        "linkedin_url": m.get("linkedin_url") or "",
    }


# ---------------------------------------------------------------------------
# Queue loading / normalization
# ---------------------------------------------------------------------------
QUEUE_FIELDS = ["apollo_contact_id", "name", "first_name", "last_name", "linkedin_url", "old_email",
                "old_domain", "old_title", "old_company", "new_title", "new_company", "new_domain",
                "sfdc_contact_id", "event_date"]


def normalize_apollo_contact(c):
    """Raw Apollo contact (with contact_job_change_event) -> flat queue row, or None if the
    event is missing, already processed, or dismissed."""
    ev = c.get("contact_job_change_event") or {}
    if not ev or ev.get("is_processed") or ev.get("is_dismissed"):
        return None
    old_email = c.get("email") or ""
    return {
        "apollo_contact_id": c.get("id") or "",
        "name": c.get("name") or "",
        "first_name": c.get("first_name") or "",
        "last_name": c.get("last_name") or "",
        "linkedin_url": c.get("linkedin_url") or "",
        "old_email": old_email,
        "old_domain": norm_domain(ev.get("old_organization_domain")) or email_domain(old_email),
        "old_title": ev.get("old_title") or "",
        "old_company": ev.get("old_organization_name") or "",
        "new_title": ev.get("title") or "",
        "new_company": ev.get("new_organization_name") or "",
        "new_domain": norm_domain(ev.get("new_organization_domain")),
        "sfdc_contact_id": c.get("salesforce_contact_id") or "",
        "event_date": (ev.get("created_at") or "")[:10],
    }


def normalize_flat(r):
    """Already-flat row (JSON/CSV export using QUEUE_FIELDS names)."""
    row = {k: (r.get(k) or "") for k in QUEUE_FIELDS}
    row["new_domain"] = norm_domain(row["new_domain"])
    row["old_domain"] = norm_domain(row["old_domain"]) or email_domain(row["old_email"])
    row["event_date"] = row["event_date"][:10]
    return row


def load_queue_file(path):
    """Load a JSON (list, or {"contacts": [...]}) or CSV export. Accepts raw Apollo contacts
    or flat rows. Returns normalized rows."""
    if path.lower().endswith(".csv"):
        with open(path, newline="") as f:
            items = list(csv.DictReader(f))
    else:
        with open(path) as f:
            data = json.load(f)
        items = data.get("contacts", []) if isinstance(data, dict) else data
    rows = []
    for it in items:
        if "contact_job_change_event" in it:
            r = normalize_apollo_contact(it)
        else:
            r = normalize_flat(it)
        if r:
            rows.append(r)
    return rows
