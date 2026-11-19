import unittest

from freshcheck import (ApiError, live_identify, brand_from_page, clean_profile_urls, enrich_all, flag_agent_rows, reg_domain,
                        resolve_company)


class CleanUrls(unittest.TestCase):
    def test_keeps_valid_and_normalizes(self):
        ok, bad = clean_profile_urls(["https://www.linkedin.com/in/Jane-Doe/?utm=x", "linkedin.com/in/bob"])
        self.assertEqual(ok, ["https://www.linkedin.com/in/jane-doe", "https://www.linkedin.com/in/bob"])
        self.assertEqual(bad, [])

    def test_rejects_empty_slug_and_repairs_stray_space(self):
        ok, bad = clean_profile_urls(["https://www.linkedin.com/in/", "https://www.linkedin.com/in/ tod-f-92 Vanity URL name"])
        self.assertEqual(ok, ["https://www.linkedin.com/in/tod-f-92"])
        self.assertEqual(bad, ["https://www.linkedin.com/in/"])

    def test_dedupes(self):
        ok, _ = clean_profile_urls(["linkedin.com/in/a", "https://www.linkedin.com/in/A/"])
        self.assertEqual(ok, ["https://www.linkedin.com/in/a"])


class EnrichAll(unittest.TestCase):
    def test_batches_of_25_and_isolates_a_rejected_url(self):
        calls = []

        def api(urls):
            calls.append(list(urls))
            if "https://www.linkedin.com/in/poison" in urls:
                return 400, {"error": {"type": "invalid_request"}}
            return 200, [{"matched_on": u, "match_status": "matched", "matches": [{}]} for u in urls]

        urls = [f"linkedin.com/in/p{i}" for i in range(30)] + ["linkedin.com/in/poison"]
        out = enrich_all(api, urls)
        self.assertEqual(len(calls[0]), 25)
        self.assertEqual(out["rejected"], ["https://www.linkedin.com/in/poison"])
        self.assertEqual(len(out["results"]), 30)


class Resolve(unittest.TestCase):
    def test_domain_hit_needs_no_fallback(self):
        r = resolve_company("acme.com", identify=lambda **k: [{"matched_on": "acme.com", "matches": [{"id": 1, "name": "Acme", "domain": "acme.com"}]}],
                            fetch=lambda d: self.fail("no fetch needed"))
        self.assertEqual((r["id"], r["how"]), (1, "domain"))

    def test_moved_company_found_by_old_domain_redirect(self):
        def identify(domains=None, names=None):
            if domains:
                return [{"matched_on": domains[0], "matches": []}]
            return [{"matched_on": names[0], "matches": [
                {"id": 9, "name": "Hexclave", "domain": None},
                {"id": 7, "name": "Hexclave (YC S24)", "domain": "stack-auth.com"},
                {"id": 8, "name": "HexClad", "domain": "hexclad.com"}]}]

        pages = {"hexclave.com": {"final_domain": "hexclave.com", "title": "Hexclave — open-source auth", "site_name": ""},
                 "stack-auth.com": {"final_domain": "hexclave.com", "title": "", "site_name": ""},
                 "hexclad.com": {"final_domain": "hexclad.com", "title": "", "site_name": ""}}
        r = resolve_company("hexclave.com", identify=identify, fetch=pages.get)
        self.assertEqual((r["id"], r["how"], r["stale_domain"]), (7, "name+redirect", "stack-auth.com"))

    def test_no_redirect_proof_means_unresolved_not_a_guess(self):
        def identify(domains=None, names=None):
            if domains:
                return [{"matched_on": domains[0], "matches": []}]
            return [{"matched_on": names[0], "matches": [{"id": 3, "name": "Haystack", "domain": "haystackapp.io"}]}]

        pages = {"haystack.sh": {"final_domain": "haystack.sh", "title": "Haystack: agents", "site_name": ""},
                 "haystackapp.io": {"final_domain": "haystackapp.io", "title": "", "site_name": ""}}
        r = resolve_company("haystack.sh", identify=identify, fetch=pages.get)
        self.assertIsNone(r["id"])
        self.assertEqual(r["how"], "unresolved")


class LiveIdentifyErrors(unittest.TestCase):
    def test_rate_limit_waits_then_succeeds(self):
        replies = [{"status": 429, "body": {"error": {}}, "headers": {"x-ratelimit-reset": "7"}},
                   {"status": 200, "body": [{"matched_on": "a.com", "matches": []}], "headers": {}}]
        waits = []
        ident = live_identify(lambda m, p, b, tag: replies.pop(0), sleep=waits.append)
        self.assertEqual(ident(domains=["a.com"]), [{"matched_on": "a.com", "matches": []}])
        self.assertEqual(waits, [7.0])

    def test_error_is_raised_never_read_as_no_match(self):
        ident = live_identify(lambda m, p, b, tag: {"status": 429, "body": {"error": {}}, "headers": {}},
                              sleep=lambda s: None, retries=1)
        with self.assertRaises(ApiError):
            ident(domains=["a.com"])
        with self.assertRaises(ApiError):
            resolve_company("a.com", identify=ident, fetch=lambda d: None)


class Brand(unittest.TestCase):
    def test_site_name_wins_then_title_head(self):
        self.assertEqual(brand_from_page({"site_name": "Janeen", "title": "Prior authorizations, handled"}), "Janeen")
        self.assertEqual(brand_from_page({"site_name": "", "title": "Hexclave — The open-source platform"}), "Hexclave")
        self.assertEqual(brand_from_page({"site_name": "", "title": "Haystack: let your agent see"}), "Haystack")

    def test_reg_domain(self):
        self.assertEqual(reg_domain("www.app.hexclave.com"), "hexclave.com")
        self.assertEqual(reg_domain("shop.example.co.uk"), "example.co.uk")


class AgentRows(unittest.TestCase):
    def test_flags_rows_violating_a_hard_constraint(self):
        answer = {"constraints": [{"id": "c1", "importance": "hard"}, {"id": "c2", "importance": "soft"}],
                  "rows": [{"id": 1, "constraints": [{"constraint_id": "c1", "assessment": "satisfied"}]},
                           {"id": 2, "constraints": [{"constraint_id": "c1", "assessment": "violated"}]},
                           {"id": 3, "constraints": [{"constraint_id": "c2", "assessment": "violated"}]},
                           {"id": 4, "constraints": []}]}
        kept, flagged = flag_agent_rows(answer)
        self.assertEqual([r["id"] for r in kept], [1, 3, 4])
        self.assertEqual([(r["id"], r["violated_hard"]) for r in flagged], [(2, ["c1"])])


if __name__ == "__main__":
    unittest.main()
