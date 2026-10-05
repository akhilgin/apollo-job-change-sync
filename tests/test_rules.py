import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from jobchange.rules import accept_match, dedupe_queue, is_actionable, norm_domain, norm_linkedin
from jobchange.stages import build_contact_record, match_cost

NEW, OLD = "acme.test", "oldco.test"


def m(email="jane@acme.test", status="verified", org="acme.test"):
    return {"email": email, "email_status": status, "org_domain": org}


class AcceptRule(unittest.TestCase):
    def test_happy_path(self):
        self.assertEqual(accept_match(m(), NEW, OLD), (True, "ok"))

    def test_no_match(self):
        self.assertEqual(accept_match(None, NEW, OLD), (False, "no_match"))

    def test_blank_email(self):
        self.assertEqual(accept_match(m(email=""), NEW, OLD), (False, "no_email"))

    def test_unavailable_status(self):
        self.assertEqual(accept_match(m(status="unavailable"), NEW, OLD), (False, "email_unavailable"))

    def test_guessed_email_is_allowed(self):
        self.assertTrue(accept_match(m(status="extrapolated"), NEW, OLD)[0])

    def test_email_still_on_old_domain(self):
        self.assertEqual(accept_match(m(email="jane@oldco.test", org="acme.test"), NEW, OLD), (False, "email_on_old_domain"))

    def test_email_domain_differs_from_website_domain(self):
        # org matches the new website domain, email uses a different mail domain: accept
        self.assertTrue(accept_match(m(email="jane@acme-mail.test", org="acme.test"), NEW, OLD)[0])

    def test_org_missing_but_email_domain_matches(self):
        self.assertTrue(accept_match(m(org=""), NEW, OLD)[0])

    def test_landed_at_different_company(self):
        self.assertEqual(accept_match(m(email="jane@other.test", org="other.test"), NEW, OLD), (False, "domain_mismatch"))

    def test_empty_new_domain_never_accepts(self):
        self.assertFalse(accept_match(m(email="jane@x.test", org=""), "", OLD)[0])

    def test_case_and_www_insensitive(self):
        self.assertTrue(accept_match(m(email="Jane@ACME.test", org="www.Acme.test"), "https://acme.test/", OLD)[0])


class Normalize(unittest.TestCase):
    def test_domain(self):
        self.assertEqual(norm_domain("https://www.Acme.test/about"), "acme.test")

    def test_linkedin_variants_match(self):
        a = norm_linkedin("https://www.linkedin.com/in/jane-example/")
        b = norm_linkedin("http://linkedin.com/in/Jane-Example?trk=x")
        self.assertEqual(a, b)

    def test_actionable(self):
        self.assertEqual(is_actionable({"new_domain": ""}), (False, "no_new_domain"))
        self.assertEqual(is_actionable({"new_domain": "gmail.com"}, ["gmail.com"]), (False, "generic_new_domain"))
        self.assertEqual(is_actionable({"new_domain": "a.test", "old_domain": "a.test"}), (False, "same_domain"))
        self.assertEqual(is_actionable({"new_domain": "a.test", "old_domain": "b.test"}), (True, "ok"))


class Dedupe(unittest.TestCase):
    def test_same_linkedin_collapses_and_keeps_latest(self):
        rows = [
            {"apollo_contact_id": "1", "linkedin_url": "https://www.linkedin.com/in/x/", "event_date": "2026-01-01", "sfdc_contact_id": "A"},
            {"apollo_contact_id": "2", "linkedin_url": "http://linkedin.com/in/X", "event_date": "2026-02-01", "sfdc_contact_id": "B"},
            {"apollo_contact_id": "3", "linkedin_url": "https://www.linkedin.com/in/y", "event_date": "2026-01-01", "sfdc_contact_id": "C"},
        ]
        kept, dropped = dedupe_queue(rows)
        self.assertEqual([r["apollo_contact_id"] for r in kept], ["2", "3"])
        self.assertEqual([r["apollo_contact_id"] for r in dropped], ["1"])
        self.assertEqual(kept[0]["all_sfdc_contact_ids"], ["A", "B"])

    def test_no_linkedin_falls_back_to_name_and_domain(self):
        rows = [
            {"apollo_contact_id": "1", "name": "Pat Q", "new_domain": "a.test"},
            {"apollo_contact_id": "2", "name": "pat q", "new_domain": "https://a.test"},
            {"apollo_contact_id": "3", "name": "Pat Q", "new_domain": "b.test"},
        ]
        kept, _ = dedupe_queue(rows)
        self.assertEqual(len(kept), 2)


class Payloads(unittest.TestCase):
    S = {"contact_lead_source": "Apollo", "contact_linkedin_field": "LinkedIn_Profile__c"}

    def test_refuses_blank_email(self):
        with self.assertRaises(ValueError):
            build_contact_record({"name": "A B", "match": {"email": ""}}, "001", self.S)

    def test_builds_record(self):
        e = {"name": "Jane Example", "new_title": "VP", "match": {"email": "j@acme.test", "first_name": "Jane", "last_name": "Example"}}
        r = build_contact_record(e, "001X", self.S)
        self.assertEqual((r["Email"], r["AccountId"], r["LeadSource"], r["Title"]), ("j@acme.test", "001X", "Apollo", "VP"))

    def test_cost_estimate(self):
        self.assertEqual(match_cost(25), (3, 25))


if __name__ == "__main__":
    unittest.main()
