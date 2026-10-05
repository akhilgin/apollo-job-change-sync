import argparse
import os

from . import stages
from .config import load_config


def build_parser():
    p = argparse.ArgumentParser(prog="jobchange", description="Handle Apollo job-change events in Salesforce without overwriting history.")
    p.add_argument("--config", help="path to config.json (default: ./config.json if present, else built-in example defaults)")
    sub = p.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch", help="load the job-change queue (Apollo API or a JSON/CSV export), dedupe by LinkedIn URL")
    f.add_argument("--input", help="JSON or CSV export")
    f.add_argument("--apollo", action="store_true", help="fetch from the Apollo API (needs APOLLO_API_KEY)")
    f.add_argument("--out")

    r = sub.add_parser("review", help="read-only Salesforce join: existing contact at new employer? target account?")
    r.add_argument("--queue"); r.add_argument("--out")

    m = sub.add_parser("match", help="Apollo bulk match for new work emails; prints cost estimate, needs --yes to spend")
    m.add_argument("--queue"); m.add_argument("--review"); m.add_argument("--out")
    m.add_argument("--limit", type=int, help="match at most N people (good for a first small run)")
    m.add_argument("--yes", action="store_true", help="confirm credit spend")

    e = sub.add_parser("execute", help="create accounts/contacts and flag old contacts (dry run unless --live)")
    e.add_argument("--queue"); e.add_argument("--matches")
    e.add_argument("--live", action="store_true", help="write to Salesforce (backup is written first)")
    e.add_argument("--offline", action="store_true", help="dry run without any Salesforce lookups (assumes nothing exists yet)")

    rp = sub.add_parser("report", help="summarize match results and the latest execute log")
    rp.add_argument("--matches"); rp.add_argument("--log")
    return p


def main(argv=None):
    a = build_parser().parse_args(argv)
    cfg = load_config(a.config)
    out = cfg["output_dir"]
    q = getattr(a, "queue", None) or os.path.join(out, "queue.json")
    if a.cmd == "fetch":
        stages.fetch(cfg, a.input, a.apollo, a.out)
    elif a.cmd == "review":
        stages.review(cfg, q, a.out)
    elif a.cmd == "match":
        stages.match(cfg, q, a.review, a.out, a.yes, a.limit)
    elif a.cmd == "execute":
        stages.execute(cfg, q, a.matches or os.path.join(out, "matches.json"), a.live, a.offline)
    elif a.cmd == "report":
        stages.report(cfg, a.matches, a.log)
    return 0
