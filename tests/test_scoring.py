from __future__ import annotations

import unittest

import score as scoring
from score import SiteReport


def make_rep(**facts_overrides):
    facts = {
        "https": True,
        "cert_error": False,
        "mixed_content": False,
        "has_viewport": True,
        "viewport_content": "width=device-width, initial-scale=1",
        "zoom_blocked": False,
        "media_queries": 30,
        "fixed_widths": 0,
        "css_fetched": 1,
        "inline_media": 0,
        "legacy_markers": [],
        "html_kb": 100,
        "ttfb_s": 0.5,
        "lang": "en",
        "alt_ratio": 1.0,
        "emails": ["hi@example.ie"],
        "phones": ["tel:+353"],
        "has_contact_form": True,
        "modern_framework": False,
        "agency_credit": None,
        "copyright_year": 2020,
        "business_live": True,
        "last_modified": None,
        "title": "",
        "meta_description": "",
        "rest": {
            "posts": 10,
            "pages": 10,
            "products": 0,
            "newest_date": "2020-01-01T00:00:00",
        },
    }
    facts.update(facts_overrides)
    rep = SiteReport(target="example.ie")
    rep.status = "scored"
    rep.wordpress = True
    rep.requested = "example.ie"
    rep.registrable = "example.ie"
    rep.domain = "example.ie"
    rep.facts = facts
    return rep


def reason_points(rep, label):
    return sum(r.points for r in rep.reasons if r.label == label)


class KeywordFitTests(unittest.TestCase):
    def test_solicitor_scores_as_small_business(self):
        rep = make_rep()
        scoring.score(rep, {"name": "Callan Tansey Solicitors"})
        self.assertEqual(reason_points(rep, "small_business_category"), 14)

    def test_two_categories_score_higher(self):
        rep = make_rep()
        scoring.score(rep, {"name": "Galway Craft Butchers"})
        self.assertEqual(reason_points(rep, "small_business_category"), 18)

    def test_category_column_also_counts(self):
        rep = make_rep()
        scoring.score(rep, {"name": "Murphy & Sons", "category": "Accountants"})
        self.assertEqual(reason_points(rep, "small_business_category"), 14)

    def test_page_title_is_used(self):
        rep = make_rep(title="O'Donnell Waters | Solicitors in Galway")
        scoring.score(rep, {})
        self.assertEqual(reason_points(rep, "small_business_category"), 14)

    def test_short_word_does_not_match_inside_another(self):
        # "pub" must not match "public".
        self.assertEqual(scoring.extract_target_keywords("public library")["weak"], [])
        self.assertEqual(scoring.extract_target_keywords("The Old Pub")["weak"], ["pub"])

    def test_plural_still_matches(self):
        self.assertEqual(
            scoring.extract_target_keywords("Duggan & Power Chartered Accountants")["strong"],
            ["accountant"])

    def test_enterprise_is_penalised(self):
        rep = make_rep()
        scoring.score(rep, {"name": "Atlantic Technological University"})
        penalties = {p.label: p.points for p in rep.penalties}
        self.assertEqual(penalties.get("enterprise_org"), -15)
        self.assertIn("enterprise", rep.flags)


class SizeFitTests(unittest.TestCase):
    def test_small_site_gets_points(self):
        rep = make_rep()
        scoring.score(rep, {})
        self.assertEqual(reason_points(rep, "small_site"), 8)

    def test_large_site_is_demoted(self):
        rep = make_rep(rest={"posts": 900, "pages": 200, "products": 0,
                             "newest_date": "2020-01-01T00:00:00"})
        scoring.score(rep, {})
        self.assertEqual(reason_points(rep, "large_site"), -8)

    def test_big_ecommerce_loses_the_no_shop_bonus(self):
        rep = make_rep(rest={"posts": 5, "pages": 5, "products": 900,
                             "newest_date": "2020-01-01T00:00:00"})
        scoring.score(rep, {})
        self.assertEqual(reason_points(rep, "no_ecommerce"), 0)
        self.assertEqual(reason_points(rep, "small_ecommerce"), 0)


class DefectTests(unittest.TestCase):
    def test_broken_certificate_is_a_defect(self):
        rep = make_rep(cert_error=True, https=False)
        scoring.score(rep, {})
        self.assertEqual(reason_points(rep, "certificate_invalid"), 12)

    def test_no_responsive_breakpoints_is_a_defect(self):
        rep = make_rep(media_queries=0, css_fetched=2)
        scoring.score(rep, {})
        self.assertEqual(reason_points(rep, "no_mobile_layout"), 12)

    def test_unknown_css_is_not_assumed_broken(self):
        rep = make_rep(media_queries=0, css_fetched=0, inline_media=0)
        scoring.score(rep, {})
        self.assertEqual(reason_points(rep, "no_mobile_layout"), 0)

    def test_zoom_blocked_is_mild(self):
        rep = make_rep(zoom_blocked=True)
        scoring.score(rep, {})
        self.assertEqual(reason_points(rep, "zoom_blocked"), 6)

    def test_page_builder_alone_is_not_legacy(self):
        rep = make_rep(legacy_markers=["page-builder:elementor", "not-block-editor"])
        scoring.score(rep, {})
        self.assertEqual(reason_points(rep, "dated_front_end"), 3)


class PenaltyTests(unittest.TestCase):
    def test_recent_redesign_needs_corroboration(self):
        # Only the copyright is current: one of three signals, no penalty.
        rep = make_rep(copyright_year=scoring.NOW.year,
                       rest={"posts": 5, "pages": 5, "products": 0,
                             "newest_date": "2020-01-01T00:00:00"})
        scoring.score(rep, {})
        self.assertNotIn("recent_redesign", {p.label for p in rep.penalties})

    def test_recent_redesign_fires_on_two_signals(self):
        rep = make_rep(copyright_year=scoring.NOW.year,
                       rest={"posts": 5, "pages": 5, "products": 0,
                             "newest_date": "2026-10-01T00:00:00"},
                       last_modified="Mon, 01 Sep 2026 00:00:00 GMT")
        scoring.score(rep, {})
        self.assertIn("recent_redesign", {p.label for p in rep.penalties})

    def test_modern_framework_is_penalised(self):
        rep = make_rep(modern_framework=True)
        scoring.score(rep, {})
        penalties = {p.label: p.points for p in rep.penalties}
        self.assertEqual(penalties.get("modern_framework"), -12)

    def test_agency_credit_is_only_a_small_penalty(self):
        rep = make_rep(agency_credit="site by")
        scoring.score(rep, {})
        penalties = {p.label: p.points for p in rep.penalties}
        self.assertEqual(penalties.get("agency_built"), -4)


class TierTests(unittest.TestCase):
    def test_keyword_defect_combination_reaches_tier_a(self):
        # 12 defect + 14 keyword + 8 small + 4 no-shop + neglect + reachability
        rep = make_rep(cert_error=True, https=False)
        scoring.score(rep, {"name": "Galway Solicitors"})
        self.assertGreaterEqual(rep.score, 48)
        self.assertEqual(rep.tier, "A")

    def test_non_wordpress_short_circuits(self):
        rep = make_rep()
        rep.status = "not_wordpress"
        scoring.score(rep, {})
        self.assertEqual(rep.tier, "not_wordpress")
        self.assertIsNone(rep.score)


if __name__ == "__main__":
    unittest.main()
