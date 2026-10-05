"""Pipeline stages: fetch, review, match, execute, report. Each takes a config dict and paths."""
import collections
import csv
import json
import math
import os
import time

from . import apollo
from .rules import accept_match, dedupe_queue, email_domain, is_actionable, norm_domain, norm_linkedin
from .sfdc import Salesforce, chunks, in_list, soql_str


def stamp():
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def read_json(path):
    with open(path) as f:
        return json.load(f)


def write_json(path, obj):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f, indent=1)


# ---------------------------------------------------------------- fetch
def fetch(cfg, input_path=None, from_apollo=False, out=None):
    if bool(input_path) == bool(from_apollo):
        raise SystemExit("fetch: pass exactly one of --input FILE or --apollo")
    if input_path:
        rows = apollo.load_queue_file(input_path)
    else:
        raw = apollo.fetch_job_changes(cfg)
        rows = [r for r in (apollo.normalize_apollo_contact(c) for c in raw) if r]
    kept, dropped = dedupe_queue(rows)
    out = out or os.path.join(cfg["output_dir"], "queue.json")
    write_json(out, kept)
    print("events loaded: {} | duplicates dropped: {} | people in queue: {}".format(len(rows), len(dropped), len(kept)))
    print("wrote", out)
    return kept


# ---------------------------------------------------------------- review (read-only)
def review(cfg, queue_path, out=None, sf=None):
    s = cfg["salesforce"]
    queue = read_json(queue_path)
    sf = sf or Salesforce(s["api_version"], s["target_org"])
    tgt = s.get("account_target_field")
    dom_f, li_f = s["account_domain_field"], s["contact_linkedin_field"]

    old_ids = sorted({i for r in queue for i in (r.get("all_sfdc_contact_ids") or [r.get("sfdc_contact_id")]) if i})
    old = {}
    for ch in chunks(old_ids, 200):
        for r in sf.query("SELECT Id, {chg}, {stg} FROM Contact WHERE Id IN ({ids})".format(
                chg=s["contact_changed_job_field"], stg=s["contact_stage_field"], ids=in_list(ch))):
            old[r["Id"]] = r

    doms = sorted({norm_domain(r["new_domain"]) for r in queue} - {""})
    acct = {}
    for ch in chunks(doms, 200):
        cols = "Id, Name, " + dom_f + (", " + tgt if tgt else "")
        for r in sf.query("SELECT {} FROM Account WHERE {} IN ({})".format(cols, dom_f, in_list(ch))):
            acct.setdefault((r.get(dom_f) or "").lower(), r)

    people = collections.defaultdict(list)    # account id -> contacts
    for ch in chunks([a["Id"] for a in acct.values()], 200):
        for c in sf.query("SELECT Id, FirstName, LastName, Email, AccountId, {} FROM Contact WHERE AccountId IN ({})".format(li_f, in_list(ch))):
            people[c["AccountId"]].append(c)

    rows = []
    for r in queue:
        nd = norm_domain(r["new_domain"])
        a = acct.get(nd)
        existing = None
        if a:
            li = norm_linkedin(r.get("linkedin_url"))
            fn, ln = (r.get("first_name") or "").lower(), (r.get("last_name") or "").lower()
            for c in people.get(a["Id"], []):
                same_li = li and norm_linkedin(c.get(li_f)) == li
                same_name = fn and ln and (c.get("FirstName") or "").lower() == fn and (c.get("LastName") or "").lower() == ln
                if same_li or same_name:
                    existing = c["Id"]
                    break
        ids = r.get("all_sfdc_contact_ids") or ([r["sfdc_contact_id"]] if r.get("sfdc_contact_id") else [])
        rows.append({
            "apollo_contact_id": r["apollo_contact_id"], "name": r["name"], "new_company": r["new_company"], "new_domain": nd,
            "new_account_in_sfdc": bool(a), "new_account_is_target": bool(a and tgt and a.get(tgt)),
            "existing_contact_at_new_account": existing or "",
            "old_contacts_found": sum(1 for i in ids if i in old),
            "old_contacts_already_flagged": sum(1 for i in ids if i in old and old[i].get(s["contact_changed_job_field"])),
        })
    out = out or os.path.join(cfg["output_dir"], "review.json")
    write_json(out, rows)
    n = len(rows)
    print("queue: {} people".format(n))
    print("new employer already an account: {}".format(sum(r["new_account_in_sfdc"] for r in rows)))
    if tgt:
        print("new employer is a target account: {}".format(sum(r["new_account_is_target"] for r in rows)))
    print("already has a contact at new employer (skipped by `match`): {}".format(sum(1 for r in rows if r["existing_contact_at_new_account"])))
    print("wrote", out)
    return rows


# ---------------------------------------------------------------- match (spends credits)
def match_cost(n_people, batch=10):
    """Return (batches, max_credits). Upper bound: one credit per person whose email is revealed;
    verify against your plan's pricing."""
    return math.ceil(n_people / batch), n_people


def match(cfg, queue_path, review_path=None, out=None, yes=False, limit=None):
    queue = read_json(queue_path)
    skip_existing = set()
    review_path = review_path or os.path.join(cfg["output_dir"], "review.json")
    if os.path.exists(review_path):
        skip_existing = {r["apollo_contact_id"] for r in read_json(review_path) if r["existing_contact_at_new_account"]}
    else:
        print("note: no review file found; not skipping people who already exist in Salesforce")

    out = out or os.path.join(cfg["output_dir"], "matches.json")
    done = {m["apollo_contact_id"]: m for m in (read_json(out) if os.path.exists(out) else [])}
    results, todo = list(done.values()), []
    reasons = collections.Counter()
    for r in queue:
        if r["apollo_contact_id"] in done:
            continue
        ok, why = is_actionable(r, cfg["generic_domains"])
        if ok and r["apollo_contact_id"] in skip_existing:
            ok, why = False, "contact_exists_at_new_account"
        if not ok:
            reasons[why] += 1
            results.append(_entry(r, None, False, why))
        elif not (r.get("linkedin_url") or (r.get("first_name") and r.get("last_name"))):
            reasons["not_enough_identity"] += 1
            results.append(_entry(r, None, False, "not_enough_identity"))
        else:
            todo.append(r)
    if limit:
        todo = todo[:limit]
    batches, credits = match_cost(len(todo))
    print("people to match: {} | already matched: {} | skipped before matching: {}".format(len(todo), len(done), dict(reasons)))
    print("estimated cost: up to {} credits in {} API calls (upper bound, 1 credit per person; check your Apollo plan)".format(credits, batches))
    if not todo:
        write_json(out, results)
        return results
    if not yes:
        write_json(out, results)
        print("Nothing spent. Re-run with --yes to call Apollo.")
        return results
    for ch in chunks(todo, 10):
        details = [{k: v for k, v in (("linkedin_url", r["linkedin_url"]), ("first_name", r["first_name"]),
                                      ("last_name", r["last_name"]), ("domain", r["new_domain"])) if v} for r in ch]
        for r, m in zip(ch, apollo.bulk_match(cfg, details)):
            flat = apollo.match_to_result(m)
            ok, why = accept_match(flat, r["new_domain"], r["old_domain"])
            results.append(_entry(r, flat, ok, why))
        write_json(out, results)          # checkpoint after every call so a crash does not re-spend
    c = collections.Counter(e["reason"] for e in results)
    print("done. accepted: {} | by reason: {}".format(sum(e["accept"] for e in results), dict(c)))
    print("wrote", out)
    return results


def _entry(r, flat, ok, why):
    return {"apollo_contact_id": r["apollo_contact_id"], "name": r["name"], "first_name": r["first_name"],
            "last_name": r["last_name"], "linkedin_url": r["linkedin_url"], "new_company": r["new_company"],
            "new_domain": r["new_domain"], "old_domain": r["old_domain"], "new_title": r["new_title"],
            "sfdc_contact_id": r.get("sfdc_contact_id", ""), "match": flat, "accept": ok, "reason": why}


# ---------------------------------------------------------------- execute
def build_contact_record(entry, account_id, s):
    """Salesforce Contact payload for an accepted match. Refuses to build one without an email."""
    m = entry.get("match") or {}
    email = (m.get("email") or "").strip()
    if not email:
        raise ValueError("refusing to create a contact without an email: " + entry.get("name", "?"))
    parts = (entry.get("name") or "").split()
    fn = m.get("first_name") or entry.get("first_name") or (parts[0] if parts else "")
    ln = m.get("last_name") or entry.get("last_name") or (parts[-1] if len(parts) > 1 else "") or "Unknown"
    rec = {"attributes": {"type": "Contact"}, "FirstName": fn[:40], "LastName": ln[:80], "Email": email,
           "Title": (m.get("title") or entry.get("new_title") or "")[:128], "AccountId": account_id,
           "LeadSource": s["contact_lead_source"]}
    li = m.get("linkedin_url") or entry.get("linkedin_url")
    if li:
        rec[s["contact_linkedin_field"]] = li
    return rec


def _account_domain(cfg, e):
    if cfg["account_domain_basis"] == "email":
        return email_domain(e["match"]["email"])
    return norm_domain(e["new_domain"])


def execute(cfg, queue_path, matches_path, live=False, offline=False, sf=None):
    s = cfg["salesforce"]
    queue, matches = read_json(queue_path), read_json(matches_path)
    generic = {norm_domain(d) for d in cfg["generic_domains"]}
    accepted = [e for e in matches if e["accept"] and _account_domain(cfg, e) and _account_domain(cfg, e) not in generic]
    print("accepted matches: {}".format(len(accepted)))
    if live and offline:
        raise SystemExit("--live and --offline cannot be combined")
    sf = None if offline else (sf or Salesforce(s["api_version"], s["target_org"]))
    dom_f = s["account_domain_field"]

    # skip emails already in Salesforce
    existing = {}
    if sf:
        for ch in chunks(sorted({e["match"]["email"].lower() for e in accepted}), 200):
            for r in sf.query("SELECT Id, Email FROM Contact WHERE Email IN ({})".format(in_list(ch))):
                existing[(r["Email"] or "").lower()] = r["Id"]
    to_create = [e for e in accepted if e["match"]["email"].lower() not in existing]
    print("email already in Salesforce (skipped): {} | to create: {}".format(len(accepted) - len(to_create), len(to_create)))

    # accounts
    doms = sorted({_account_domain(cfg, e) for e in to_create})
    acct = {}
    if sf:
        for ch in chunks(doms, 200):
            for r in sf.query("SELECT Id, {d} FROM Account WHERE {d} IN ({v})".format(d=dom_f, v=in_list(ch))):
                acct.setdefault((r[dom_f] or "").lower(), r["Id"])
    missing = [d for d in doms if d not in acct]
    name_for = {}
    for e in to_create:
        name_for.setdefault(_account_domain(cfg, e), e["new_company"] or _account_domain(cfg, e))
    print("accounts found: {} | accounts to create: {}".format(len(acct), len(missing)))

    # old contacts to flag
    to_flag_ids = sorted({i for r in queue for i in (r.get("all_sfdc_contact_ids") or [r.get("sfdc_contact_id")]) if i})
    old_state = {}
    if sf:
        for ch in chunks(to_flag_ids, 200):
            for r in sf.query("SELECT Id, FirstName, LastName, Email, Title, AccountId, {c}, {st} FROM Contact WHERE Id IN ({i})".format(
                    c=s["contact_changed_job_field"], st=s["contact_stage_field"], i=in_list(ch))):
                old_state[r["Id"]] = r
        to_flag = [i for i, r in old_state.items()
                   if not (r.get(s["contact_changed_job_field"]) and r.get(s["contact_stage_field"]) == s["contact_stage_value"])]
    else:
        to_flag = to_flag_ids
    print("old contacts to flag: {}".format(len(to_flag)))

    if not live:
        print("\nDRY RUN: nothing written. Re-run with --live to execute.")
        return None

    bdir = cfg["backup_dir"]
    os.makedirs(bdir, exist_ok=True)
    st = stamp()
    bpath = os.path.join(bdir, "backup_{}.json".format(st))
    write_json(bpath, {"old_contacts_before": old_state, "planned_contacts": to_create, "planned_accounts": missing})
    print("backup written:", bpath)

    log = {"stamp": st, "accounts": [], "contacts": [], "flags": [], "name_matched_accounts": []}
    # 1) accounts
    for ch in chunks(missing, 200):
        recs = [{"attributes": {"type": "Account"}, "Name": name_for[d][:255], dom_f: d, "Website": "https://" + d} for d in ch]
        for d, rec, r in zip(ch, recs, sf.composite("POST", recs)):
            if not r.get("success") and any(x.get("statusCode") == "DUPLICATES_DETECTED" for x in r.get("errors") or []):
                # duplicate rule matched by name though the domain differs: attach to the existing account
                found = sf.query("SELECT Id FROM Account WHERE Name = {} LIMIT 1".format(soql_str(rec["Name"])))
                if found:
                    acct[d] = found[0]["Id"]
                    log["name_matched_accounts"].append({"domain": d, "name": rec["Name"], "account_id": found[0]["Id"]})
                    r = {"success": True, "id": found[0]["Id"], "errors": []}
            log["accounts"].append({"domain": d, "success": r.get("success"), "id": r.get("id"), "errors": r.get("errors")})
            if r.get("success"):
                acct[d] = r["id"]
    print("accounts created or matched: {}/{}".format(sum(1 for a in log["accounts"] if a["success"]), len(missing)))

    # 2) contacts
    creatable = [e for e in to_create if _account_domain(cfg, e) in acct]
    for ch in chunks(creatable, 200):
        recs = [build_contact_record(e, acct[_account_domain(cfg, e)], s) for e in ch]
        for e, r in zip(ch, sf.composite("POST", recs)):
            log["contacts"].append({"email": e["match"]["email"], "name": e["name"], "success": r.get("success"),
                                    "id": r.get("id"), "errors": r.get("errors"), "apollo_id": e["apollo_contact_id"]})
    print("contacts created: {}/{}".format(sum(1 for c in log["contacts"] if c["success"]), len(creatable)))

    # 3) flag old contacts (never touch their title, company, or email)
    if cfg["flag_scope"] == "created":
        ok_ids = {c["apollo_id"] for c in log["contacts"] if c["success"]}
        keep = {i for r in queue if r["apollo_contact_id"] in ok_ids
                for i in (r.get("all_sfdc_contact_ids") or [r.get("sfdc_contact_id")]) if i}
        to_flag = [i for i in to_flag if i in keep]
    for ch in chunks(to_flag, 200):
        recs = [{"attributes": {"type": "Contact"}, "Id": i, s["contact_changed_job_field"]: True,
                 s["contact_stage_field"]: s["contact_stage_value"]} for i in ch]
        for i, r in zip(ch, sf.composite("PATCH", recs)):
            log["flags"].append({"id": i, "success": r.get("success"), "errors": r.get("errors")})
    print("old contacts flagged: {}/{}".format(sum(1 for f in log["flags"] if f["success"]), len(to_flag)))

    lpath = os.path.join(cfg["output_dir"], "execute_log_{}.json".format(st))
    write_json(lpath, log)
    print("log written:", lpath)
    return log


# ---------------------------------------------------------------- report
def report(cfg, matches_path=None, log_path=None):
    matches_path = matches_path or os.path.join(cfg["output_dir"], "matches.json")
    if os.path.exists(matches_path):
        m = read_json(matches_path)
        print("== match results ({} people)".format(len(m)))
        for k, v in collections.Counter(e["reason"] for e in m).most_common():
            print("  {:<32}{}".format(k, v))
    if not log_path:
        logs = sorted(f for f in os.listdir(cfg["output_dir"]) if f.startswith("execute_log_")) if os.path.isdir(cfg["output_dir"]) else []
        log_path = os.path.join(cfg["output_dir"], logs[-1]) if logs else None
    if not log_path:
        print("no execute log found (dry runs do not write one)")
        return
    log = read_json(log_path)
    print("\n== execute log", os.path.basename(log_path))
    for k in ("accounts", "contacts", "flags"):
        rows = log[k]
        ok = sum(1 for r in rows if r["success"])
        print("  {:<10}{}/{} succeeded".format(k, ok, len(rows)))
        errs = collections.Counter(((r.get("errors") or [{}])[0].get("statusCode") or "error") for r in rows if not r["success"])
        for e, n in errs.items():
            print("    failed: {} x{}".format(e, n))
    nm = log.get("name_matched_accounts") or []
    if nm:
        print("  accounts attached by name (duplicate rule blocked a new account; review these):")
        for a in nm:
            print("    {} -> {}".format(a["name"], a["account_id"]))
