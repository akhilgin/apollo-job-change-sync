"""Config loading. Defaults are example values; copy config.example.json to config.json and edit."""
import json
import os

DEFAULTS = {
    "salesforce": {
        "api_version": "v60.0",
        "target_org": None,                      # sf CLI alias/username; None = CLI default org
        "contact_changed_job_field": "Changed_Job__c",      # checkbox on Contact
        "contact_stage_field": "Stage__c",                  # picklist/text on Contact
        "contact_stage_value": "Changed Job",
        "contact_linkedin_field": "LinkedIn_Profile__c",    # URL/text on Contact
        "contact_lead_source": "Apollo",
        "account_domain_field": "Domain__c",                # text on Account
        "account_target_field": None,                       # optional checkbox on Account, e.g. "Target_Account__c"
    },
    "apollo": {
        "base_url": "https://api.apollo.io/api/v1",
        "per_page": 100,
        "max_pages": 50,
    },
    "generic_domains": ["gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com", "linkedin.com"],
    # "org": create/find the Account by the matched organization's website domain (default)
    # "email": create/find the Account by the matched email's domain. Use this if your org has
    # automation that re-parents contacts to the account matching their email domain.
    "account_domain_basis": "org",
    "flag_scope": "all",                         # "all" = flag every old contact in the queue; "created" = only when a new contact was made
    "output_dir": "output",
    "backup_dir": "backups",
}


def _merge(base, over):
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


def load_config(path=None):
    path = path or ("config.json" if os.path.exists("config.json") else None)
    if not path:
        return json.loads(json.dumps(DEFAULTS))
    with open(path) as f:
        return _merge(DEFAULTS, json.load(f))
