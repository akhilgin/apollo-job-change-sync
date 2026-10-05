"""Pure functions: normalization, dedupe, and the match accept rule. No I/O, no network."""
import re


def norm_domain(value):
    """'https://www.Acme.test/path' -> 'acme.test'. Empty string if nothing usable."""
    v = (value or "").strip().lower()
    v = re.sub(r"^[a-z]+://", "", v)
    v = v.split("/")[0].split("?")[0]
    if v.startswith("www."):
        v = v[4:]
    return v


def email_domain(email):
    e = (email or "").strip().lower()
    return e.rsplit("@", 1)[1] if "@" in e else ""


def norm_linkedin(url):
    """Canonical form for comparing LinkedIn profile URLs. Empty string if none."""
    v = (url or "").strip().lower()
    v = re.sub(r"^[a-z]+://", "", v)
    v = v.split("?")[0].split("#")[0].rstrip("/")
    if v.startswith("www."):
        v = v[4:]
    return v


def person_key(row):
    """Identity key for dedupe: LinkedIn URL when present, else name + new domain."""
    li = norm_linkedin(row.get("linkedin_url"))
    if li:
        return "li:" + li
    name = (row.get("name") or "{} {}".format(row.get("first_name", ""), row.get("last_name", ""))).strip().lower()
    return "nm:{}|{}".format(name, norm_domain(row.get("new_domain")))


def dedupe_queue(rows):
    """Collapse duplicate people. Keeps the row with the latest event_date (ties: first seen).

    The kept row gets `all_sfdc_contact_ids`: every Salesforce contact id seen for that
    person, so every stale record can be flagged later, not just the survivor.
    Returns (kept, dropped).
    """
    best, order = {}, []
    ids = {}
    for r in rows:
        k = person_key(r)
        if r.get("sfdc_contact_id"):
            ids.setdefault(k, [])
            if r["sfdc_contact_id"] not in ids[k]:
                ids[k].append(r["sfdc_contact_id"])
        if k not in best:
            best[k] = r
            order.append(k)
        elif (r.get("event_date") or "") > (best[k].get("event_date") or ""):
            best[k] = r
    kept, dropped = [], []
    kept_ids = set()
    for k in order:
        row = dict(best[k])
        row["all_sfdc_contact_ids"] = ids.get(k, [])
        kept.append(row)
        kept_ids.add(id(best[k]))
    dropped = [r for r in rows if id(r) not in kept_ids]
    return kept, dropped


def is_actionable(row, generic_domains=()):
    """Cheap pre-check before spending any Apollo credit. Returns (ok, reason)."""
    nd = norm_domain(row.get("new_domain"))
    if not nd:
        return False, "no_new_domain"
    if nd in {norm_domain(d) for d in generic_domains}:
        return False, "generic_new_domain"
    if nd == norm_domain(row.get("old_domain")):
        return False, "same_domain"
    return True, "ok"


def accept_match(match, new_domain, old_domain):
    """Decide whether an Apollo people-match result may be used to create a contact.

    Accept only if ALL hold:
      1. the match has an email
      2. email_status is not 'unavailable'
      3. the email domain is not the old company's domain
      4. the matched organization's primary_domain == new_domain, OR the email domain == new_domain
    Rule 4 uses OR on purpose: a company's email domain can differ from its website domain,
    so filtering on email domain alone would reject real moves.

    `match` is a dict like {"email", "email_status", "org_domain"} or None.
    Returns (accepted: bool, reason: str).
    """
    if not match:
        return False, "no_match"
    email = (match.get("email") or "").strip().lower()
    if not email or "@" not in email:
        return False, "no_email"
    if (match.get("email_status") or "").lower() == "unavailable":
        return False, "email_unavailable"
    ed = email_domain(email)
    nd, od = norm_domain(new_domain), norm_domain(old_domain)
    if od and ed == od:
        return False, "email_on_old_domain"
    org = norm_domain(match.get("org_domain"))
    if nd and (org == nd or ed == nd):
        return True, "ok"
    return False, "domain_mismatch"
