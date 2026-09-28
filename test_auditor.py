"""Offline checks so the auditor can be verified without a network."""

from __future__ import annotations

import unittest

from auditor import (
    _is_public_host,
    analyze_html,
    normalize_url,
    parse_robots,
    robots_blocks,
    score_page,
)


SAMPLE = """<!doctype html><html><head>
<title>Hi</title>
</head><body><img src="a.jpg"><p>short</p><a href="/about">About</a></body></html>"""


class AuditorTests(unittest.TestCase):
    def test_normalize_adds_https(self) -> None:
        self.assertEqual(normalize_url("example.com/docs"), "https://example.com/docs")

    def test_rejects_private_network(self) -> None:
        with self.assertRaises(ValueError):
            _is_public_host("10.1.1.1")

    def test_allows_loopback(self) -> None:
        _is_public_host("127.0.0.1")

    def test_missing_basics_are_flagged(self) -> None:
        page = analyze_html("https://example.com/", 200, "https://example.com/", SAMPLE)
        codes = {item["code"] for item in page["issues"]}
        self.assertEqual(page["title"], "Hi")
        self.assertIn("description", codes)
        self.assertIn("h1", codes)
        self.assertIn("viewport", codes)
        self.assertIn("alt", codes)
        self.assertEqual(page["images_missing_alt"], 1)
        self.assertLess(page["score"], 80)

    def test_complete_page_scores_higher(self) -> None:
        html = """<!doctype html><html lang="en"><head>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Garden tools for small balconies and patios</title>
        <meta name="description" content="A practical guide to pots, soil, and watering for a balcony garden, written for people with one sunny railing and a weekend.">
        <link rel="canonical" href="https://example.com/garden">
        <meta property="og:title" content="Balcony garden tools">
        </head><body>
        <h1>Balcony garden tools</h1>
        <h2>Pots</h2>
        <img src="pot.jpg" alt="Clay pot">
        <p>%s</p>
        </body></html>""" % ("word " * 180)
        page = analyze_html("https://example.com/garden", 200, "https://example.com/garden", html)
        self.assertGreaterEqual(page["score"], 90)
        self.assertEqual(page["issues"], [])

    def test_robots(self) -> None:
        rules, maps = parse_robots("User-agent: *\nDisallow: /private\nSitemap: https://example.com/sitemap.xml\n")
        self.assertEqual(maps, ["https://example.com/sitemap.xml"])
        self.assertTrue(robots_blocks("/private/page", rules))
        self.assertFalse(robots_blocks("/blog", rules))

    def test_http_error_is_high(self) -> None:
        score, issues = score_page({
            "status": 500, "title": "Ok title that is long enough here", "description": "x" * 80,
            "h1": ["Hello"], "lang": "en", "viewport": True, "canonical": "https://e.com/",
            "words": 400, "images_missing_alt": 0, "h2": 1, "og_title": "T", "noindex": False,
        })
        self.assertTrue(any(item["code"] == "http" for item in issues))
        self.assertLess(score, 100)


if __name__ == "__main__":
    unittest.main()
