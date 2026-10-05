# apollo-job-change-sync

Process Apollo job-change events into Salesforce without overwriting the old contact, creating blank-email records, or making duplicates.

## The problem

People change jobs, and your CRM rots. A contact you've worked for years now sits at a company they left, with an email that bounces.

The obvious fix is to update that contact with the new job. That's the wrong move:

- The old contact carries history: activity, campaign membership, opportunity roles, attribution. Overwrite it and that history now points at the wrong company.
- Someone who was a champion at Company A and is now at Company B is two different relationships, and you want to report on both.

Vendor auto-processing of job changes has its own problems. Apollo's in-app "Create new contact" pushes records to Salesforce with no dedupe of its own. In practice that can mean contacts with blank emails, the same person created twice in the same second, and accounts created with nothing in them.

This tool does the work with explicit rules and checkpoints so you can see what will happen before it happens.

## Approach

For each person Apollo flags as having changed jobs:

1. Leave the old contact's job data alone. Set a "changed job" checkbox and a stage/status value on it, nothing else.
2. Look up the person's work email at the new company through Apollo's bulk people match.
3. If the match passes the accept rule below, find or create the new employer's Account (by domain) and create a new Contact there with the new title and email.
4. If it doesn't pass, create nothing. The old contact is still flagged.

### Accept rule

A match is used only if all of these hold:

| Check | Why |
|---|---|
| Email is present | A contact without an email is not worth a record, and blank-email contacts are what make a CRM messy. |
| `email_status` is not `unavailable` | Apollo is telling you it can't stand behind the address. Guessed (`extrapolated`) addresses are allowed. |
| Email domain is not the old company's domain | Apollo sometimes returns the stale address. That is the old job, not the new one. |
| Matched organization's `primary_domain` equals the new domain, **or** the email domain equals the new domain | Confirms the person landed where the event says. It is an OR because many companies email from a domain that differs from their website domain. Filtering on email domain alone would throw away real moves. |

Before matching (and before spending credits), people are also skipped when the new domain is missing, is a generic mail/social domain, or equals the old domain.

### Dedupe

Job-change queues contain the same person more than once. People are deduplicated by normalized LinkedIn URL first (falling back to name plus new domain). The latest event wins, and every Salesforce contact id seen for that person is kept so all stale records get flagged, not just the survivor.

## Pipeline

```
 Apollo API  ──┐
               ├─> fetch ──> queue.json (deduped)
 JSON / CSV  ──┘                 │
                                 v
                     review  (read-only Salesforce join)
                       - contact already at new employer?
                       - new employer already an account / target?
                                 │  review.json
                                 v
                     match   (Apollo bulk match, 10 per call)
                       - prints estimated credit cost
                       - spends nothing without --yes
                       - applies the accept rule
                                 │  matches.json
                                 v
                     execute (dry run by default)
                       - --live: backup, then
                         accounts -> contacts -> flag old contacts
                                 │  execute_log_<time>.json
                                 v
                     report
```

## Quick start

Python 3.9+, standard library only.

```bash
cp config.example.json config.json     # edit the field names for your org

# 1. Load the queue. From a file (works with the included fake sample):
python -m jobchange fetch --input sample_data/job_changes_sample.json
#    or from the Apollo API:
export APOLLO_API_KEY=...              # from your environment / secrets manager
python -m jobchange fetch --apollo

# 2. Read-only look at Salesforce (needs auth, see below)
python -m jobchange review

# 3. See the cost, then spend
python -m jobchange match --limit 20            # prints estimate, spends nothing
python -m jobchange match --limit 20 --yes      # spends credits

# 4. Dry run, then write
python -m jobchange execute
python -m jobchange execute --live

# 5. Summary
python -m jobchange report
```

To try the execute stage without any Salesforce access, use the fake data:

```bash
python -m jobchange fetch --input sample_data/job_changes_sample.json
python -m jobchange execute --matches sample_data/matches_sample.json --offline
```

`--offline` skips all Salesforce lookups and assumes nothing exists yet, so the counts are only illustrative.

Run the tests (no network):

```bash
python -m unittest discover -s tests
```

### Authentication

- Salesforce: either set `SF_INSTANCE_URL` and `SF_ACCESS_TOKEN`, or log in with the Salesforce CLI (`sf org login web`); the tool runs `sf org display --json` (add `--target-org` via `salesforce.target_org` in config or `SF_TARGET_ORG`).
- Apollo: `APOLLO_API_KEY` in the environment, sent as `X-Api-Key`.

Don't put keys in `config.json` or commit them. `config.json`, `.env`, `backups/` and `output/` are gitignored.

## Config reference

`config.json` is merged over the defaults, so you only need the keys you change. All Salesforce names in `config.example.json` are example values; replace them with your own API names.

| Key | Meaning |
|---|---|
| `salesforce.contact_changed_job_field` | Checkbox on Contact set to true on the old contact |
| `salesforce.contact_stage_field` | Stage/status field on Contact |
| `salesforce.contact_stage_value` | Value written to that field on the old contact |
| `salesforce.contact_linkedin_field` | Contact field holding the LinkedIn URL (written on create, used by `review` to find existing contacts) |
| `salesforce.contact_lead_source` | `LeadSource` for new contacts |
| `salesforce.account_domain_field` | Account field holding the website domain; used to find and create accounts |
| `salesforce.account_target_field` | Optional Account checkbox (e.g. target account) shown in `review`; `null` to skip |
| `salesforce.api_version`, `salesforce.target_org` | REST API version; `sf` CLI org alias |
| `apollo.base_url`, `per_page`, `max_pages` | Apollo REST settings for `fetch --apollo` |
| `generic_domains` | Domains never used to create accounts (webmail, social) |
| `account_domain_basis` | `org` (default): account domain is the matched organization's domain. `email`: use the email's domain. Use `email` if Salesforce automation in your org re-parents contacts to the account matching their email domain, which otherwise leaves empty accounts behind. |
| `flag_scope` | `all` flags every old contact in the queue; `created` flags only those with a new contact created |
| `output_dir`, `backup_dir` | Where stage files and backups go |

### Input format for `--input`

JSON (a list, or `{"contacts": [...]}`) or CSV. Rows can be raw Apollo contacts (with a `contact_job_change_event` object) or flat rows with these columns: `apollo_contact_id, name, first_name, last_name, linkedin_url, old_email, old_domain, old_title, old_company, new_title, new_company, new_domain, sfdc_contact_id, event_date`. See `sample_data/job_changes_sample.json`.

## Safety design

- Dry run is the default for `execute`. Nothing is written without `--live`.
- `--live` writes a JSON backup first: the current state of every old contact it will touch, plus the planned accounts and contacts.
- `match` prints the estimated credit cost and does nothing until you pass `--yes`. It checkpoints `matches.json` after every API call, so a crash or re-run doesn't pay twice for the same person.
- A contact is never created without an email. The payload builder raises if there isn't one.
- The old contact only gets the checkbox and the stage value. Title, company, and email are never touched.
- Contacts whose email already exists in Salesforce are skipped.
- Writes use composite sObject collections in batches of 200 with `allOrNone=false`, and every per-record result is logged.
- Salesforce duplicate rules can block a new Account by name even when the domain is different. When that happens the tool looks up the existing account with that name, attaches the contact to it, and lists it in `report` so you can review the pairing.

## Limitations

- The Apollo job-change filter is unverified. `fetch --apollo` sends `{"contact_job_changed": true}` to `/contacts/search`, isolated in `job_change_filter()` in `jobchange/apollo.py`. Check the parameter name against Apollo's API docs, and note that availability may depend on your plan. If it doesn't work, export the queue from Apollo and use `fetch --input`.
- `match` assumes Apollo returns bulk-match results in the same order as the request. It aborts the batch if the counts differ, but it cannot detect a reorder.
- The credit estimate is an upper bound of one credit per person. Check Apollo's pricing for your plan; email reveals may be billed differently.
- Account matching is by domain only, plus the name fallback after a duplicate-rule block. Name collisions with an account that has no domain can still produce a second account for the same company.
- Existing-contact detection in `review` uses LinkedIn URL or exact first and last name at the new account. Nicknames and name changes are missed.
- Only Contacts and Accounts are handled. It doesn't touch Leads, opportunities, or Apollo-side stages.
- Not tested against a live org in this form. Run `review` and a small `--limit` batch in a sandbox first.

## License

MIT
