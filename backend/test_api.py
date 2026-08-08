import unittest
from unittest.mock import AsyncMock, patch

from backend import api


class SearchBehaviorTests(unittest.IsolatedAsyncioTestCase):
    def test_short_business_identifier_counts_toward_relevance(self) -> None:
        self.assertGreaterEqual(api.relevance("4T Manufacturing", ["4T Manufacturing LLC"]), 2)

    def test_relevant_search_snippet_can_supply_attributed_contacts(self) -> None:
        item = {"url": "https://example.com/contact", "title": "Example LLC", "snippet": "Example LLC call 415-555-2671", "provider": "ddgs_brave"}
        result = api.snippet_result(item, ["Example LLC"], "contact", "HTTP 403", 0.2)
        self.assertIsNotNone(result)
        self.assertEqual(result["method"], "search_snippet")
        self.assertTrue(result["phones"])

    def test_universal_query_modes(self) -> None:
        self.assertEqual(api.search_mode(api.SearchRequest(query="potato")), "general")
        self.assertEqual(api.search_mode(api.SearchRequest(query="John Doe from 33139")), "contact")
        self.assertEqual(api.search_mode(api.SearchRequest(query="4T Manufacturing LLC")), "contact")
        self.assertEqual(api.search_mode(api.SearchRequest(query="305-555-0100")), "contact")
        self.assertEqual(api.search_subject(["984 LLC"]), '"984 LLC"')
        self.assertEqual(api.search_subject(["potato"]), "potato")

    def test_html_contact_surfaces_and_phone_type(self) -> None:
        html = """
        <html><body><a href="mailto:sales@example.com">Email</a>
        <script type="application/ld+json">{"telephone":"+44 7911 123456"}</script>
        </body></html>
        """
        text = api.visible_text(html)
        phones, emails = api.extract_contacts(api.extraction_surface(html, text))
        self.assertIn("sales@example.com", emails)
        self.assertTrue(phones)
        self.assertIn("line_type", phones[0])
        self.assertIn("region", phones[0])
        self.assertIsNone(api.normalize_phone("13107470"))

    def test_zip_constraint_blocks_generic_person_directory_contacts(self) -> None:
        item = {"url": "https://example.com/john-doe", "title": "John Doe", "snippet": "People directory", "provider": "test"}
        html = '<a href="tel:+1 415 555 2671">Call</a>'
        source = api.source_result(item, html, "John Doe directory", "static", ["John Doe from 33139"], "contact", 0.01)
        self.assertFalse(source["identity_match"])
        self.assertEqual(source["phones"], [])

    def test_known_phone_query_keeps_only_requested_number(self) -> None:
        item = {"url": "https://example.com/directory", "title": "Eastland Car Service", "snippet": "7183826868", "provider": "test"}
        html = "<p>Call 718-382-6868 or our other offices 917-336-1193 and 718-888-5555.</p>"
        source = api.source_result(item, html, "Eastland Car Service 7183826868", "static", ["7183826868"], "contact", 0.01)
        self.assertEqual([phone["number"] for phone in source["phones"]], ["+17183826868"])

    def test_known_email_query_keeps_only_requested_email(self) -> None:
        item = {"url": "https://example.com/contact", "title": "Example LLC", "snippet": "sales@example.com", "provider": "test"}
        html = "<p>sales@example.com and billing@example.com</p>"
        source = api.source_result(item, html, "Example LLC sales@example.com", "static", ["sales@example.com"], "contact", 0.01)
        self.assertEqual(source["emails"], ["sales@example.com"])

    def test_source_details_and_directory_mailbox_filter(self) -> None:
        item = {
            "url": "https://www.mapquest.com/us/idaho/example-123",
            "title": "Example Homes LLC, 984 W Corporate Ln, Nampa, ID 83651-1743, US - MapQuest",
            "snippet": "Example Homes LLC specializes in custom home building. The owner is Jane Doe.",
            "provider": "test",
        }
        source = api.source_result(item, "<p>Call 208-466-2500 help@mapquest.com</p>", item["snippet"], "static", ["Example Homes LLC"], "contact", 0.01)
        self.assertEqual(source["emails"], [])
        self.assertEqual(source["details"]["address"], "984 W Corporate Ln, Nampa, ID 83651-1743")
        self.assertIn("custom home building", source["details"]["business_type"])
        self.assertEqual(source["details"]["owner"], "Jane Doe")
        phone_index = {**item, "url": "https://thephoneindex.com/prefix-718-382"}
        self.assertEqual(api.filter_source_emails(["me@thephoneindex.com"], phone_index, ["Example Homes LLC"]), [])

    def test_business_phrase_does_not_match_number_inside_unrelated_address(self) -> None:
        item = {
            "url": "https://www.mapquest.com/us/idaho/shervik-signature-homes-llc-288519777",
            "title": "Shervik Signature Homes LLC, 984 W Corporate Ln, Nampa, ID 83651-1743, US - MapQuest",
            "snippet": "Shervik Signature Homes specializes in custom home building.",
            "provider": "test",
        }
        source = api.source_result(item, "<p>Call 208-466-2500</p>", item["snippet"], "static", ["984 LLC"], "contact", 0.01)
        self.assertFalse(source["identity_match"])
        self.assertEqual(source["phones"], [])
        self.assertEqual(source["details"], {})

    def test_business_phrase_requires_token_boundaries(self) -> None:
        self.assertFalse(api.identity_constraints_met("SG984, LLC company profile", ["984 LLC"]))
        self.assertFalse(api.identity_constraints_met("PI DER-984 LLC company profile", ["984 LLC"]))
        self.assertTrue(api.identity_constraints_met("984, LLC company profile", ["984 LLC"]))

    def test_json_ld_fields_are_extracted_before_page_context(self) -> None:
        html = '''
        <script type="application/ld+json">
        {"@type":"AutomotiveBusiness","name":"Example Auto LLC","telephone":"+1 305-555-0100",
         "email":"service@exampleauto.com","address":{"streetAddress":"12 Main St","addressLocality":"Miami",
         "addressRegion":"FL","postalCode":"33139"},"founder":{"name":"Jane Doe"},
         "description":"Independent auto repair shop."}
        </script>
        '''
        details = api.extract_business_details({"title":"Example Auto LLC", "snippet":"© 2025 Google LLC."}, html, "")
        self.assertEqual(details["business_name"], "Example Auto LLC")
        self.assertEqual(details["business_type"], "Automotive Business")
        self.assertEqual(details["address"], "12 Main St, Miami, FL, 33139")
        self.assertEqual(details["owner"], "Jane Doe")
        self.assertEqual(details["summary"], "Independent auto repair shop.")
        phones, emails = api.extract_contacts(api.extraction_surface(html, ""))
        self.assertEqual(phones[0]["line_type"], "mobile or landline")
        self.assertIn("service@exampleauto.com", emails)

    async def test_contact_discovery_adds_public_facebook_query(self) -> None:
        calls = []

        def fake_ddgs(query: str, proxy: str, backend: str):
            calls.append(query)
            return []

        with patch.object(api, "ddgs_search", side_effect=fake_ddgs), patch.object(api, "provider_chain", return_value=[("test", None, "bing")]):
            await api.discover('"Example Auto LLC"', "", target=3, mode="contact")
        self.assertTrue(any("site:facebook.com" in query for query in calls))

    async def test_general_query_succeeds_with_sources_without_contacts(self) -> None:
        discovered = [{"url": "https://example.com/", "title": "Potato", "snippet": "Potato reference", "provider": "test"}]
        scraped = [{**discovered[0], "status": "scraped", "method": "static", "relevance": 5, "phones": [], "emails": [], "duration_seconds": 0.01}]
        with patch.object(api, "discover", new=AsyncMock(return_value=(discovered, [{"provider": "test", "status": "complete", "results": 1}]))), patch.object(
            api, "scrape_sources", new=AsyncMock(return_value=scraped)
        ):
            result = await api.run_search(api.SearchRequest(query="potato", verify_email_domains=False))
        self.assertEqual(result["mode"], "general")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["scraped_count"], 1)

    async def test_contacts_include_their_source_urls(self) -> None:
        discovered = [{"url": "https://example.com/contact", "title": "Example", "snippet": "Example LLC", "provider": "test"}]
        phone = {"number": "+14155552671", "extension": "", "line_type": "mobile or landline", "region": "US"}
        scraped = [{**discovered[0], "status": "scraped", "method": "static", "relevance": 5, "identity_match": True, "phones": [phone], "emails": ["sales@example.com"], "duration_seconds": 0.01}]
        with patch.object(api, "discover", new=AsyncMock(return_value=(discovered, [{"provider": "test", "status": "complete", "results": 1}]))), patch.object(
            api, "scrape_sources", new=AsyncMock(return_value=scraped)
        ):
            result = await api.run_search(api.SearchRequest(query="Example LLC", verify_email_domains=False))
        self.assertEqual(result["phones"][0]["source_urls"], ["https://example.com/contact"])
        self.assertEqual(result["emails"][0]["source_urls"], ["https://example.com/contact"])

    async def test_contact_results_drop_unconfirmed_sources(self) -> None:
        discovered = [
            {"url": "https://example.com/match", "title": "Example LLC", "snippet": "Example LLC", "provider": "test"},
            {"url": "https://example.com/wrong", "title": "Unrelated LLC", "snippet": "Unrelated", "provider": "test"},
        ]
        scraped = [
            {**discovered[0], "status": "scraped", "method": "static", "relevance": 5, "identity_match": True, "phones": [], "emails": [], "duration_seconds": 0.01},
            {**discovered[1], "status": "scraped", "method": "static", "relevance": 1, "identity_match": False, "phones": [], "emails": [], "duration_seconds": 0.01},
        ]
        with patch.object(api, "discover", new=AsyncMock(return_value=(discovered, [{"provider": "test", "status": "complete", "results": 2}]))), patch.object(
            api, "scrape_sources", new=AsyncMock(return_value=scraped)
        ):
            result = await api.run_search(api.SearchRequest(query="Example LLC", verify_email_domains=False))
        self.assertEqual([source["url"] for source in result["sources"]], ["https://example.com/match"])
        self.assertEqual(result["discarded_source_count"], 1)

    def test_required_routes_remain_available(self) -> None:
        methods_by_path = {route.path: route.methods for route in api.app.routes if hasattr(route, "methods")}
        self.assertIn("GET", methods_by_path["/health"])
        self.assertIn("POST", methods_by_path["/search"])
        self.assertIn("POST", methods_by_path["/search/stream"])
        self.assertIn("POST", methods_by_path["/batch"])


if __name__ == "__main__":
    unittest.main()
