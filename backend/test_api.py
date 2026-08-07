import unittest
from unittest.mock import AsyncMock, patch

from backend import api


class SearchBehaviorTests(unittest.IsolatedAsyncioTestCase):
    def test_short_business_identifier_counts_toward_relevance(self) -> None:
        self.assertGreaterEqual(api.relevance("4T Manufacturing", ["4T Manufacturing LLC"]), 2)

    def test_universal_query_modes(self) -> None:
        self.assertEqual(api.search_mode(api.SearchRequest(query="potato")), "general")
        self.assertEqual(api.search_mode(api.SearchRequest(query="John Doe from 33139")), "contact")
        self.assertEqual(api.search_mode(api.SearchRequest(query="4T Manufacturing LLC")), "contact")
        self.assertEqual(api.search_mode(api.SearchRequest(query="305-555-0100")), "contact")

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

    def test_zip_constraint_blocks_generic_person_directory_contacts(self) -> None:
        item = {"url": "https://example.com/john-doe", "title": "John Doe", "snippet": "People directory", "provider": "test"}
        html = '<a href="tel:+1 415 555 2671">Call</a>'
        source = api.source_result(item, html, "John Doe directory", "static", ["John Doe from 33139"], "contact", 0.01)
        self.assertFalse(source["identity_match"])
        self.assertEqual(source["phones"], [])

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

    def test_required_routes_remain_available(self) -> None:
        methods_by_path = {route.path: route.methods for route in api.app.routes if hasattr(route, "methods")}
        self.assertIn("GET", methods_by_path["/health"])
        self.assertIn("POST", methods_by_path["/search"])
        self.assertIn("POST", methods_by_path["/search/stream"])
        self.assertIn("POST", methods_by_path["/batch"])


if __name__ == "__main__":
    unittest.main()
