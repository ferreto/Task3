import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import impressum
from company_status import CompanyStatus


class NormalizeUrlTests(unittest.TestCase):
    def test_keeps_scheme(self):
        self.assertEqual(impressum.normalize_url("https://example.com"), "https://example.com")

    def test_adds_scheme(self):
        self.assertEqual(impressum.normalize_url("www.example.com"), "https://www.example.com")
        self.assertEqual(impressum.normalize_url("example.com/path"), "https://example.com/path")

    def test_strips_whitespace(self):
        self.assertEqual(impressum.normalize_url("  example.com  "), "https://example.com")

    def test_empty(self):
        self.assertIsNone(impressum.normalize_url(""))
        self.assertIsNone(impressum.normalize_url("   "))
        self.assertIsNone(impressum.normalize_url(None))


class FindImpressumTests(unittest.TestCase):
    def test_visible_text_match(self):
        html = '<a href="/impressum">Impressum</a>'
        self.assertEqual(
            impressum.find_impressum_url(html, "https://example.com/"),
            "https://example.com/impressum",
        )

    def test_case_insensitive(self):
        html = '<a href="/rechtliches">IMPRESSUM</a>'
        self.assertEqual(
            impressum.find_impressum_url(html, "https://example.com/"),
            "https://example.com/rechtliches",
        )

    def test_href_fallback(self):
        html = '<a href="/legal/impressum.html">Legal notice</a>'
        self.assertEqual(
            impressum.find_impressum_url(html, "https://example.com/"),
            "https://example.com/legal/impressum.html",
        )

    def test_relative_resolution(self):
        html = '<a href="../impressum.html">Impressum</a>'
        self.assertEqual(
            impressum.find_impressum_url(html, "https://example.com/de/"),
            "https://example.com/impressum.html",
        )

    def test_ignores_unusable_links(self):
        html = """
        <a href="mailto:info@example.com">Impressum</a>
        <a href="javascript:void(0)">Impressum</a>
        <a href="#">Impressum</a>
        <a href="/impressum">Impressum</a>
        """
        self.assertEqual(
            impressum.find_impressum_url(html, "https://example.com/"),
            "https://example.com/impressum",
        )

    def test_no_link(self):
        self.assertIsNone(
            impressum.find_impressum_url("<a href='/about'>About</a>", "https://example.com/")
        )


class ExtractVisibleTextTests(unittest.TestCase):
    def test_removes_non_content_tags(self):
        html = """
        <html><head><style>.x { color: red; }</style></head>
        <body>
          <script>var secret = 1;</script>
          <nav>Menu</nav>
          <header>Header</header>
          <footer>Footer</footer>
          <form>Form</form>
          <h1>Impressum</h1>
          <p>ACME GmbH</p>
          <p>Hauptstraße 1, 10115 Berlin</p>
        </body></html>
        """
        text = impressum.extract_visible_text(html)
        self.assertIn("Impressum", text)
        self.assertIn("ACME GmbH", text)
        self.assertIn("Hauptstraße 1, 10115 Berlin", text)
        for removed in ("var secret", "color: red", "Menu", "Header", "Footer", "Form"):
            self.assertNotIn(removed, text)


class StripBoilerplateTests(unittest.TestCase):
    def test_removes_boilerplate_keeps_legal(self):
        text = "\n".join(
            [
                "Cookie-Einstellungen",
                "Datenschutz",
                "AGB",
                "© 2024 ACME GmbH",
                "Menü",
                "Deutsch",
                "Impressum",
                "Angaben gemäß § 5 TMG",
                "ACME GmbH",
                "Hauptstraße 1",
                "10115 Berlin",
            ]
        )
        result = impressum.strip_boilerplate_lines(text)
        self.assertNotIn("Cookie-Einstellungen", result)
        self.assertNotIn("Datenschutz", result)
        self.assertNotIn("AGB", result)
        self.assertNotIn("© 2024", result)
        self.assertNotIn("Menü", result)
        self.assertIn("Impressum", result)
        self.assertIn("Angaben gemäß § 5 TMG", result)
        self.assertIn("Hauptstraße 1", result)


class OptimizeTextTests(unittest.TestCase):
    def test_short_text_unchanged(self):
        text = "Impressum\nACME GmbH"
        result, stats = impressum.optimize_text(text, max_chars=100)
        self.assertEqual(result, text)
        self.assertEqual(stats["method"], "unchanged")

    def test_long_text_sandwich(self):
        prefix = "Navigation " * 100
        legal = (
            "Impressum\n"
            "Angaben gemäß § 5 TMG\n"
            "ACME GmbH\n"
            "Hauptstraße 1\n"
            "10115 Berlin\n"
            "Tel: +49 30 1234567\n"
        )
        suffix = "Footertext " * 100 + "UNIQUE_TAIL_MARKER"
        text = prefix + legal + suffix

        result, stats = impressum.optimize_text(text, max_chars=1000)
        self.assertEqual(stats["method"], "sandwich")
        self.assertLess(len(result), len(text))
        self.assertIn("Impressum", result)
        self.assertIn("+49 30 1234567", result)
        self.assertNotIn("UNIQUE_TAIL_MARKER", result)
        self.assertLess(len(result), 1000)

    def test_verification_failure_uses_full_text(self):
        text = "Impressum\n" + ("Fülltext ohne rechtliche Angaben. " * 20)
        result, stats = impressum.optimize_text(text, max_chars=500)
        self.assertEqual(stats["method"], "full_text_fallback")
        self.assertEqual(result, text.strip())

    def test_extreme_length_uses_window(self):
        text = (
            "Impressum\n"
            + ("Fülltext mit Angaben. " * 100)
            + "\nTel: +49 30 1234567\n"
            + ("Ende. " * 100)
        )
        result, stats = impressum.optimize_text(text, max_chars=500)
        self.assertEqual(stats["method"], "window_fallback")
        self.assertLessEqual(len(result), 500 + len("\n...\n"))
        self.assertIn("Impressum", result)

    def test_stats_contain_token_counts(self):
        text = "Impressum\n" + ("x" * 3000)
        _, stats = impressum.optimize_text(text, max_chars=500)
        self.assertIn("original_tokens", stats)
        self.assertIn("optimized_tokens", stats)
        self.assertEqual(stats["original_tokens"], len(text) // 4)


class ParseExtractionTests(unittest.TestCase):
    VALID = {
        "company_name": "ACME GmbH",
        "postal_address": {
            "street": "Hauptstraße",
            "house_no": "1",
            "postcode": "10115",
            "city": "Berlin",
            "country": "Germany",
        },
        "handelsregister": "HRB 123456",
        "vat_number": "DE123456789",
        "phone": "+49 30 1234567",
        "email": "Info@ACME.DE",
        "responsible_person": "Max Mustermann",
    }

    def test_valid_json(self):
        data = impressum.parse_extraction(json.dumps(self.VALID))
        self.assertEqual(data["company_name"], "ACME GmbH")
        self.assertEqual(data["email"], "info@acme.de")
        self.assertEqual(data["postal_address"]["city"], "Berlin")

    def test_fenced_json(self):
        content = "```json\n" + json.dumps(self.VALID) + "\n```"
        data = impressum.parse_extraction(content)
        self.assertEqual(data["postal_address"]["street"], "Hauptstraße")

    def test_missing_keys_filled(self):
        data = impressum.parse_extraction('{"company_name": "ACME"}')
        self.assertEqual(data["company_name"], "ACME")
        for key in ("handelsregister", "vat_number", "phone", "email", "responsible_person"):
            self.assertIsNone(data[key])
        self.assertEqual(
            data["postal_address"],
            {"street": None, "house_no": None, "postcode": None, "city": None, "country": None},
        )

    def test_address_wrong_shape(self):
        data = impressum.parse_extraction('{"company_name": "ACME", "postal_address": "Berlin"}')
        self.assertIsNone(data["postal_address"]["city"])

    def test_invalid_content_raises(self):
        with self.assertRaises(ValueError):
            impressum.parse_extraction("")
        with self.assertRaises(ValueError):
            impressum.parse_extraction("   ")
        with self.assertRaises(ValueError):
            impressum.parse_extraction("[]")
        with self.assertRaises(json.JSONDecodeError):
            impressum.parse_extraction("not json")


class DeepSeekErrorClassificationTests(unittest.TestCase):
    def make_response(self, status_code=200, content=None, body=None):
        response = mock.Mock()
        response.status_code = status_code
        response.text = ""
        if body is not None:
            response.json.return_value = body
        elif content is not None:
            response.json.return_value = {
                "choices": [{"message": {"content": content}}]
            }
        return response

    def test_parse_error_is_deepseek_error(self):
        self.assertTrue(issubclass(impressum.DeepSeekParseError, impressum.DeepSeekError))

    def test_success_returns_normalized_data(self):
        response = self.make_response(content='{"company_name": "ACME GmbH"}')
        with mock.patch.object(impressum.requests, "post", return_value=response) as post:
            data = impressum.call_deepseek("text", "key")
        self.assertEqual(data["company_name"], "ACME GmbH")
        self.assertEqual(post.call_count, 1)

    def test_blank_content_raises_parse_error_after_retries(self):
        response = self.make_response(content="")
        with mock.patch.object(impressum.time, "sleep"), mock.patch.object(
            impressum.requests, "post", return_value=response
        ) as post:
            with self.assertRaises(impressum.DeepSeekParseError):
                impressum.call_deepseek("text", "key")
        self.assertEqual(post.call_count, impressum.DEEPSEEK_MAX_ATTEMPTS)

    def test_non_json_content_raises_parse_error_after_retries(self):
        response = self.make_response(content="not json")
        with mock.patch.object(impressum.time, "sleep"), mock.patch.object(
            impressum.requests, "post", return_value=response
        ) as post:
            with self.assertRaises(impressum.DeepSeekParseError):
                impressum.call_deepseek("text", "key")
        self.assertEqual(post.call_count, impressum.DEEPSEEK_MAX_ATTEMPTS)

    def test_auth_error_is_not_parse_error(self):
        response = self.make_response(status_code=401)
        with mock.patch.object(impressum.requests, "post", return_value=response) as post:
            with self.assertRaises(impressum.DeepSeekError) as ctx:
                impressum.call_deepseek("text", "key")
        self.assertNotIsInstance(ctx.exception, impressum.DeepSeekParseError)
        self.assertEqual(post.call_count, 1)

    def test_network_error_is_not_parse_error(self):
        with mock.patch.object(impressum.time, "sleep"), mock.patch.object(
            impressum.requests,
            "post",
            side_effect=impressum.requests.RequestException("boom"),
        ) as post:
            with self.assertRaises(impressum.DeepSeekError) as ctx:
                impressum.call_deepseek("text", "key")
        self.assertNotIsInstance(ctx.exception, impressum.DeepSeekParseError)
        self.assertEqual(post.call_count, impressum.DEEPSEEK_MAX_ATTEMPTS)


class StatusDecisionTests(unittest.TestCase):
    def test_no_fields(self):
        data = impressum.parse_extraction("{}")
        self.assertFalse(impressum.has_any_field(data))
        self.assertEqual(impressum.decide_status(data), CompanyStatus.PARSING_FAILED)

    def test_partial_fields(self):
        data = impressum.parse_extraction('{"company_name": "ACME"}')
        self.assertTrue(impressum.has_any_field(data))
        self.assertEqual(impressum.decide_status(data), CompanyStatus.ADDRESS_INCOMPLETE)

    def test_complete_address(self):
        data = impressum.parse_extraction(
            json.dumps(
                {
                    "company_name": "ACME",
                    "postal_address": {
                        "street": "Hauptstraße",
                        "house_no": "1",
                        "postcode": "10115",
                        "city": "Berlin",
                    },
                }
            )
        )
        self.assertTrue(impressum.is_address_complete(data))
        self.assertEqual(impressum.decide_status(data), CompanyStatus.OK)

    def test_country_not_required(self):
        data = {
            "postal_address": {
                "street": "Hauptstraße",
                "house_no": "1",
                "postcode": "10115",
                "city": "Berlin",
                "country": None,
            }
        }
        self.assertTrue(impressum.is_address_complete(data))

    def test_flatten_address(self):
        data = {
            "postal_address": {
                "street": "Hauptstraße",
                "house_no": "1",
                "postcode": "10115",
                "city": "Berlin",
                "country": "Germany",
            }
        }
        self.assertEqual(impressum.flatten_address(data), "Hauptstraße 1, 10115 Berlin, Germany")

    def test_flatten_address_skips_empty_parts(self):
        data = {"postal_address": {"street": "Hauptstraße", "city": "Berlin"}}
        self.assertEqual(impressum.flatten_address(data), "Hauptstraße, Berlin")


if __name__ == "__main__":
    unittest.main()
