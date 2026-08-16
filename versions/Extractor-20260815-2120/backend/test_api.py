import unittest
from backend import api

class CoreTests(unittest.TestCase):
    def test_column_detection_is_alias_and_case_tolerant(self):
        m=api.detect_columns(["Business Name","OWNER FIRST NAME","Owner Last Name","Phone1","Phone 2","E-mail","Approval Amount","Birth Date"])
        self.assertIn("Business Name",m["company"])
        self.assertEqual(len(m["phone"]),2)
        self.assertIn("Approval Amount",m["revenue"])
        self.assertIn("Birth Date",m["dob"])
    def test_phone_variants_and_format(self):
        p=api.normalize_phone("+1 7542555555")
        self.assertEqual(p["display"],"(754) 255-5555")
        self.assertIn("754-255-5555",api.phone_variants("7542555555"))
    def test_query_plan_expands_identity(self):
        req=api.SearchRequest(identity=api.Identity(company="Example LLC",owner="Jane Doe",zip="33139",phone="7542555555",email="jane@example.com"))
        plan=api.query_plan(req); kinds={x["kind"] for x in plan}
        self.assertTrue({"facebook","owner_zip","phone","email_domain"}.issubset(kinds))
    def test_dob_is_high_weight_identity_signal(self):
        req=api.SearchRequest(identity=api.Identity(owner="Jane Doe",dob="05/22/1976"))
        score,reasons=api.identity_score("Jane Doe born 05/22/1976",req)
        self.assertGreaterEqual(score,47)
        self.assertIn("DOB match",reasons)

    def test_main_query_phone_is_not_treated_as_company(self):
        req=api.SearchRequest(query="9175222667")
        plan=api.query_plan(req)
        kinds={x["kind"] for x in plan}
        self.assertIn("phone",kinds)
        self.assertNotIn("official",kinds)

    def test_instruction_can_target_a_public_site(self):
        req=api.SearchRequest(query="9175222667",instruction="search example.com for matching public contact information")
        plan=api.query_plan(req)
        self.assertTrue(any(x["kind"]=="user_site" and "site:example.com" in x["query"] for x in plan))

if __name__=="__main__": unittest.main()
