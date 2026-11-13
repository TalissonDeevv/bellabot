from __future__ import annotations

import unittest

from website_guard.monitor import (
    _MetaTagParser,
    _cache_busted_url,
    _strip_probe_parameter,
)


class MonitorParserTests(unittest.TestCase):
    def test_meta_parser_is_case_and_whitespace_tolerant(self) -> None:
        parser = _MetaTagParser()
        parser.feed('<META CONTENT="public-key" NAME="  Site-Sentinel-Key  ">')
        self.assertEqual(parser.meta["site-sentinel-key"], "public-key")

    def test_probe_parameter_is_removed_from_reported_url(self) -> None:
        original = "https://example.com/path?lang=pt"
        probed = _cache_busted_url(original)
        self.assertIn("_sentinel_probe=", probed)
        self.assertEqual(_strip_probe_parameter(probed), original)


if __name__ == "__main__":
    unittest.main()
