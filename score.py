#!/usr/bin/env python3
"""score.py

Score a WordPress / WooCommerce site as a redesign prospect.

Usage:
    python score.py example.com other.com
    python score.py --json example.com
    python score.py -f domains.txt
    python score.py -f domains.txt --batch 200 --workers 8
    python score.py --ignore-robots example.com

Every signal that contributes points is printed with its points, so a score can
always be explained. Only WordPress sites are written to SQLite (full row with
facts and signals); every other outcome gets a minimal row in the crawl ledger
so --batch keeps moving. Nothing else is written to disk.

Outcomes are explicit. A site is never reported as "not WordPress" unless a real
200 page was fetched and no WordPress signatures were present. Sites that refuse
automated requests (401/403/406/429/451, 503, or a JS challenge page) are
reported as "blocked" and routed to manual review rather than being dropped.

Transport is curl_cffi with a Chrome fingerprint, because a large share of hosts
reject the generic Python TLS fingerprint with a 403 while serving the same page
to a browser. This is not a WAF bypass: JS challenges (Cloudflare "Just a
moment") still come back as "blocked", and robots.txt is honoured by default.
Only /wp-json/ is read, and only when the site's robots.txt permits it.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin, urlparse, urlencode, unquote
from urllib.robotparser import RobotFileParser

from curl_cffi import requests
from curl_cffi.requests.errors import CurlError, RequestsError
from bs4 import BeautifulSoup

import store

IMPERSONATE = "chrome"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
CONNECT_TIMEOUT = 5
READ_TIMEOUT = 10
TIMEOUT = (CONNECT_TIMEOUT, READ_TIMEOUT)
RETRIES = 2

NOW = datetime.now(timezone.utc)

BLOCK_STATUSES = {401, 403, 406, 429, 451, 503}
CHALLENGE_MARKERS = (
    "just a moment", "checking your browser", "cf-browser-verification",
    "cf_chl_opt", "enable javascript and cookies to continue",
    "attention required! | cloudflare", "captcha-delivery.com",
    "incapsula incident id", "_incapsula_", "ddos protection by",
    "verifying you are human", "geo.captcha-delivery", "please wait while we verify",
)

WP_RELEASES = {
    "4.9": (2017, 11), "5.0": (2018, 12), "5.1": (2019, 2), "5.2": (2019, 5),
    "5.3": (2019, 11), "5.4": (2020, 3), "5.5": (2020, 8), "5.6": (2020, 12),
    "5.7": (2021, 3), "5.8": (2021, 7), "5.9": (2022, 1), "6.0": (2022, 5),
    "6.1": (2022, 11), "6.2": (2023, 3), "6.3": (2023, 8), "6.4": (2023, 11),
    "6.5": (2024, 4), "6.6": (2024, 7), "6.7": (2024, 11), "6.8": (2025, 4),
    "6.9": (2025, 8),
}

PREMIUM_SLUGS = (
    "elementor-pro", "divi", "bricks", "oxygen", "wpforms-pro", "gravityforms",
    "advanced-custom-fields-pro", "acf-pro", "woocommerce-subscriptions",
    "woocommerce-bookings", "woocommerce-memberships",
)

HARD_COMMERCE_PATTERNS = {
    "subscriptions": r"(?:wc|woocommerce)[-/_]?subscriptions|/wc/v\d+/subscriptions",
    "bookings": r"(?:wc|woocommerce)[-/_]?bookings",
    "memberships": r"(?:wc|woocommerce)[-/_]?memberships",
    "product-add-ons": r"(?:wc|woocommerce)[-/_]?product[-_]?add[-_]?ons",
    "composite": r"(?:wc|woocommerce)[-/_]?composite",
    "multicurrency": r"(?:wc|woocommerce)[-/_]?multicurrency",
    "multivendor": r"(?:wc|woocommerce)[-/_]?(?:multivendor|vendors)|\bwcfm\b|\bdokan\b",
    "b2b": r"(?:wc|woocommerce)[-/_]?b2b",
}

BUILDER_TOKENS = {
    "elementor": ("elementor", "elementor-widget", "data-elementor"),
    "divi": ("et_pb_", "et_builder", "et-pb", "divi-", "divi/"),
    "wpbakery": ("js_composer", "wpb_", "wpbakery", "vc_row"),
    "beaver": ("fl-builder", "fl_builder", "fl-node-"),
    "oxygen": ("oxygen-", "/oxygen/", "ct-section"),
    "bricks": ("bricks-", "/bricks/", "brxe-"),
    "breakdance": ("breakdance", "bde-"),
}

ANALYTICS_MARKERS = (
    "googletagmanager.com", "google-analytics.com", "gtag(", "ga(", "fbq(",
    "connect.facebook.net", "hotjar", "plausible.io", "matomo", "segment.com",
    "clarity.ms", "mixpanel",
)

CONSENT_MARKERS = (
    "cookieyes", "cookiebot", "onetrust", "termly", "complianz", "borlabs",
    "osano", "iubenda", "tarteaucitron", "usercentrics", "cookie-consent",
    "we use cookies",
)

FORM_MARKERS = (
    "wpcf7", "wpforms", "gform", "gravity", "ninja-forms", "formidable",
    "fluentform", "contact-form-7", "admin-ajax.php?action=", "formspree",
)

AGENCY_MARKERS = (
    "site by", "website by", "web design by", "designed by", "designed & built",
    "built by", "created by", "maintained by", "developed by", "site design by",
    "web agency", "digital agency",
)

MODERN_FRAMEWORK_MARKERS = ("data-reactroot", "__next", "_nuxt", "__nuxt", "svelte", "data-astro-cid")
SKIP_SCHEMES = ("http://www.w3.org", "http://schema.org", "http://purl.org", "http://ogp.me", "http://gmpg.org")

# --- target fit ----------------------------------------------------------
# Categories that reliably mean a small local business whose site was built
# years ago. Matched against the directory name, the page title, the meta
# description and the domain, with a leading word boundary so "accountants"
# matches "accountant" but "barnes" does not match "bar".
SMALL_BUSINESS_KEYWORDS = (
    "solicitor", "attorney", "lawyer", "legal", "notary", "patent",
    "accountant", "accounting", "bookkeep", "audit", "payroll", "tax",
    "auctioneer", "estate agent", "real estate", "architect", "engineer",
    "surveyor", "valuation", "mortgage broker", "insurance broker",
    "dentist", "dental", "orthodont", "vet", "veterinary", "physio",
    "chiropract", "osteopath", "optician", "optometrist", "pharmacy",
    "chemist", "clinic", "medical centre", "podiatr", "counsellor",
    "psychotherap", "creche", "montessori", "childcare", "preschool",
    "hairdresser", "barber", "hair salon", "beauty", "nails", "tattoo",
    "funeral", "undertaker",
    "plumber", "plumbing", "electrician", "electrical", "heating", "boiler",
    "builder", "carpentry", "joiner", "joinery", "roofing", "roofer",
    "plasterer", "tiler", "tiling", "glazing", "locksmith", "mechanic",
    "garage", "tyres", "bodyshop", "landscaping", "cleaning", "removals",
    "courier", "bakery", "bakers", "butcher", "deli", "cafe", "coffee",
    "restaurant", "bistro", "gastropub", "guesthouse", "florist", "flowers",
    "craft", "pottery", "ceramic", "jewellery", "jewelry", "knitwear",
    "woollen", "woolen", "tweed", "boutique", "bookshop", "bicycle",
    "hardware", "groomer", "kennel", "cattery", "equestrian", "saddlery",
    "taxi", "print", "signage", "photographer", "photography", "dance",
    "tutoring", "grinds", "gym", "fitness", "yoga", "pilates",
    "martial arts", "swimming", "driving school", "driving instructor",
)

# Weaker hints: real but ambiguous, so worth less than a solicitor or a bakery.
WEAK_KEYWORDS = (
    "hotel", "pub", "bar", "school", "arts", "centre", "shop", "store",
    "market", "motors", "foods", "supplies", "services",
)

# Institutions and enterprises: they buy websites, but not from a cold email.
ENTERPRISE_KEYWORDS = (
    "university", "college", "council", "authority", "government", "hospital",
    "bank", "plc", "holdings", "group", "international", "global", "foundation",
    "institute of technology", "city of", "department of", "hse", "etb",
    "technologies", "airport", "utilities", "diocese",
)

# --- scoring bands -------------------------------------------------------
CAPS = {"Defects": 45, "Target fit": 30, "Neglect": 15, "Reachability": 15}
TIERS = ((68.0, "A"), (52.0, "B"), (36.0, "C"))

SCORE_VERSION = "v2"

COMMON_SECOND_LEVEL = {
    "co.uk", "org.uk", "ac.uk", "gov.uk", "ltd.uk", "plc.uk", "me.uk", "net.uk", "sch.uk",
    "com.au", "net.au", "org.au", "gov.au", "edu.au", "asn.au", "id.au",
    "co.nz", "net.nz", "org.nz", "govt.nz", "ac.nz", "geek.nz", "school.nz",
    "co.za", "net.za", "org.za", "gov.za", "ac.za",
    "com.br", "com.mx", "com.ar", "com.co", "com.pe", "com.ve", "com.uy",
    "co.jp", "ne.jp", "or.jp", "ac.jp", "go.jp",
    "com.sg", "com.hk", "com.tw", "com.my", "com.ph", "com.vn", "com.tr",
    "co.in", "net.in", "org.in", "gen.in", "firm.in", "ind.in",
    "co.kr", "or.kr", "co.id", "co.th", "co.il", "co.ke", "com.ng", "com.gh",
    "com.eg", "com.sa", "com.ae", "com.qa", "com.kw", "com.bh", "com.om",
    "com.ua", "com.pl", "com.ro", "com.gr", "com.pt", "com.es", "com.it",
    "co.nz", "com.pk", "com.bd", "com.np", "com.lk",
}


def host_of(target: str) -> str:
    t = target if "//" in target else "//" + target
    return (urlparse(t).hostname or target).lower().strip(".")


def registrable_domain(host: str) -> str:
    host = (host or "").lower().strip(".")
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    if ".".join(parts[-2:]) in COMMON_SECOND_LEVEL:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


@dataclass
class Reason:
    axis: str
    label: str
    points: float
    detail: str = ""


@dataclass
class SiteReport:
    target: str
    url: str | None = None
    domain: str | None = None
    registrable: str | None = None
    requested: str | None = None
    status: str = "unreachable"
    http_status: int | None = None
    wordpress: bool | None = None
    wp_signatures: list[str] = field(default_factory=list)
    wp_version: str | None = None
    rest_status: str = "unreachable"
    facts: dict[str, Any] = field(default_factory=dict)
    reasons: list[Reason] = field(default_factory=list)
    penalties: list[Reason] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    score: float | None = None
    tier: str = "unscored"

    @property
    def axis_totals(self) -> dict[str, float]:
        totals: dict[str, float] = {}
        for r in self.reasons:
            totals[r.axis] = totals.get(r.axis, 0.0) + r.points
        return totals

    def to_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "registrable": self.registrable,
            "requested": self.requested,
            "url": self.url,
            "status": self.status,
            "http_status": self.http_status,
            "wordpress": self.wordpress,
            "wp_signatures": self.wp_signatures,
            "wp_version": self.wp_version,
            "rest_status": self.rest_status,
            "score": self.score,
            "tier": self.tier,
            "flags": self.flags,
            "axis_totals": {k: round(v, 1) for k, v in self.axis_totals.items()},
            "reasons": [r.__dict__ for r in self.reasons],
            "penalties": [p.__dict__ for p in self.penalties],
            "facts": self.facts,
        }


def session() -> requests.Session:
    s = requests.Session(impersonate=IMPERSONATE)
    s.headers.update({"Accept-Language": "en-US,en;q=0.9"})
    return s


def normalize(target: str) -> str:
    s = target.strip()
    if not s.startswith(("http://", "https://")):
        s = "https://" + s
    return s.rstrip("/")


def months_since(year: int, month: int) -> int:
    return (NOW.year - year) * 12 + (NOW.month - month)


def _keyword_hits(text: str, words: tuple[str, ...]) -> list[str]:
    low = (text or "").lower()
    return [w for w in words if re.search(r"(?<![a-z])" + re.escape(w), low)]


def classify_fetch_error(exc: BaseException) -> str:
    name = type(exc).__name__
    message = str(exc).lower()
    if name == "CertificateVerifyError" or "certificate" in message or "ssl" in message.split():
        return "cert"
    if "could not resolve" in message or name == "DNSError":
        return "dns"
    if "timed out" in message or "timeout" in name.lower():
        return "timeout"
    return "other"


def fetch_home(s: requests.Session, url: str) -> tuple[requests.Response | None, bool]:
    """Fetch the homepage, returning (response, tls_broken).

    A certificate that fails validation is a strong redesign signal, so it must
    not be swallowed as "unreachable". When the secure fetch fails on TLS we
    retry with verification off, record the flag, and keep crawling the page.
    """
    try:
        return s.get(url, timeout=TIMEOUT, allow_redirects=True), False
    except Exception as exc:
        if classify_fetch_error(exc) == "cert":
            try:
                return s.get(url, timeout=TIMEOUT, allow_redirects=True, verify=False), True
            except Exception:
                return None, True
        try:
            return s.get(url, timeout=TIMEOUT, allow_redirects=True), False
        except Exception:
            return None, False


def probe_responsive(s: requests.Session, style_urls: list[str],
                     inline_media: int) -> dict[str, Any]:
    """Count responsive signals in the theme CSS.

    The viewport meta is emitted by every modern theme, so it cannot separate a
    responsive site from a fixed-width one. Media queries and hardcoded pixel
    widths can.
    """
    media = inline_media
    fixed = 0
    fetched = 0
    for url in style_urls[:2]:
        r = fetch(s, url)
        if r is None or r.status_code != 200:
            continue
        css = r.text.lower()
        fetched += 1
        media += len(re.findall(r"@media", css))
        fixed += len(re.findall(r"(?:min-)?width\s*:\s*(?:9\d\d|1\d{3})px", css))
    return {"media_queries": media, "fixed_widths": fixed, "css_fetched": fetched}


def extract_target_keywords(text: str) -> dict[str, Any]:
    strong = _keyword_hits(text, SMALL_BUSINESS_KEYWORDS)
    weak = _keyword_hits(text, WEAK_KEYWORDS)
    enterprise = _keyword_hits(text, ENTERPRISE_KEYWORDS)
    return {"strong": strong, "weak": weak, "enterprise": enterprise}


def fetch(s: requests.Session, url: str, headers: dict[str, str] | None = None) -> requests.Response | None:
    for attempt in range(RETRIES + 1):
        try:
            return s.get(url, timeout=TIMEOUT, allow_redirects=True, headers=headers)
        except (RequestsError, CurlError):
            if attempt == RETRIES:
                return None
            time.sleep(0.4 * (attempt + 1))
        except Exception:
            return None
    return None


def looks_like_challenge(html: str) -> bool:
    low = html.lower()
    return any(m in low for m in CHALLENGE_MARKERS)


def wordpress_signatures(html: str, link_header: str | None) -> list[str]:
    sigs: list[str] = []
    if "/wp-content/" in html:
        sigs.append("wp-content")
    if "/wp-includes/" in html:
        sigs.append("wp-includes")
    if "wp-block-" in html or "wp-json" in html:
        sigs.append("wp-markers")
    if re.search(r'name=["\']generator["\'][^>]*wordpress', html, re.I):
        sigs.append("generator")
    if link_header and "api.w.org" in link_header:
        sigs.append("link-header")
    return sigs


def robots_for(s: requests.Session, root: str) -> tuple[RobotFileParser | None, int | None]:
    r = fetch(s, urljoin(root, "/robots.txt"))
    if r is None:
        return None, None
    if r.status_code != 200 or not r.text.strip():
        return None, r.status_code
    rp = RobotFileParser()
    rp.parse(r.text.splitlines())
    return rp, 200


def detect_builder(html: str, plugin_slugs: list[str], theme_slugs: list[str]) -> str | None:
    haystack = (html.lower(), " ".join(plugin_slugs), " ".join(theme_slugs))
    joined = " ".join(haystack)
    for name, tokens in BUILDER_TOKENS.items():
        if any(tok in joined for tok in tokens):
            return name
    return None


def legacy_era_markers(soup: BeautifulSoup, html: str, plugin_slugs: list[str],
                       theme_slugs: list[str], text_lower: str) -> list[str]:
    markers: list[str] = []

    builder = detect_builder(html, plugin_slugs, theme_slugs)
    if builder:
        markers.append(f"page-builder:{builder}")

    has_block_editor = ("wp-block-library" in text_lower
                        or re.search(r'class=["\'][^"\']*wp-block-', html) is not None)
    if not has_block_editor:
        markers.append("not-block-editor")

    if not any(tag.get("srcset") for tag in soup.find_all("img")):
        markers.append("no-srcset")

    if re.search(r"<!doctype\s+html\s+public", html, re.I):
        markers.append("old-doctype")

    return markers


def parse_homepage(html: str, base_url: str, elapsed: float, scheme: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "lxml")
    text_lower = html.lower()

    theme_slugs = sorted(p for p in set(re.findall(r"/wp-content/themes/([^/\"'?\s]+)", html))
                         if re.fullmatch(r"[a-z0-9][a-z0-9._-]*", p))
    plugin_slugs = sorted(p for p in set(re.findall(r"/wp-content/plugins/([^/\"'?\s]+)", html))
                          if re.fullmatch(r"[a-z0-9][a-z0-9._-]*", p))

    generators = [t.get("content", "") for t in
                  soup.find_all("meta", attrs={"name": re.compile("^generator$", re.I)})]
    generator = next((g for g in generators if "wordpress" in g.lower()), "")
    viewport = soup.find("meta", attrs={"name": re.compile("^viewport$", re.I)})
    viewport_content = (viewport.get("content") or "").lower() if viewport else None
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    description_tag = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
    meta_description = (description_tag.get("content") or "") if description_tag else ""
    lang = soup.html.get("lang") if soup.html else None

    imgs = soup.find_all("img")
    imgs_with_alt = [i for i in imgs if i.get("alt", "").strip()]
    alt_ratio = (len(imgs_with_alt) / len(imgs)) if imgs else None

    emails = sorted({unquote(a["href"].split(":", 1)[1].split("?")[0]).strip()
                     for a in soup.find_all("a", href=re.compile(r"^mailto:", re.I)) if a.get("href")})
    phones = [a.get("href") for a in soup.find_all("a", href=re.compile(r"^tel:", re.I))]
    forms = soup.find_all("form")
    has_contact_form = bool(forms) or any(m in text_lower for m in FORM_MARKERS)

    footer = soup.find("footer")
    footer_text = (footer.get_text(" ", strip=True) if footer else soup.get_text(" ", strip=True)[-2000:]).lower()
    agency_hit = next((m for m in AGENCY_MARKERS if m in footer_text), None)

    year_match = re.search(r"(?:©|&copy;|\(c\)|copyright)[^\d]{0,24}(20\d{2})", text_lower)
    copyright_year = int(year_match.group(1)) if year_match else None

    mixed = False
    for tag in soup.find_all(["script", "img", "iframe", "source", "link"]):
        src = tag.get("src") or tag.get("href")
        if not src or not src.startswith("http://") or src.startswith(SKIP_SCHEMES):
            continue
        if tag.name == "link" and "stylesheet" not in [str(r).lower() for r in (tag.get("rel") or [])]:
            continue
        mixed = True
        break

    asset_urls = re.findall(r'(?:src|href)=["\']([^"\']+\.(?:css|js)[^"\']*)["\']', html)
    asset_url = next((urljoin(base_url, a) for a in asset_urls if "wp-content" in a), None)
    style_urls = [urljoin(base_url, a) for a in re.findall(
        r'href=["\']([^"\']+\.css[^"\']*)["\']', html, re.I)]
    style_urls = [u for u in style_urls if "wp-content" in u][:3]
    inline_media = len(re.findall(r"@media", html, re.I))

    legacy = legacy_era_markers(soup, html, plugin_slugs, theme_slugs, text_lower)

    return {
        "theme_slugs": theme_slugs,
        "plugin_slugs": plugin_slugs,
        "generator": generator,
        "has_viewport": viewport is not None,
        "viewport_content": viewport_content,
        "zoom_blocked": bool(viewport_content
                             and ("user-scalable=no" in viewport_content
                                  or "maximum-scale=1" in viewport_content)),
        "title": title[:200],
        "meta_description": meta_description[:300],
        "style_urls": style_urls,
        "inline_media": inline_media,
        "lang": lang,
        "img_count": len(imgs),
        "alt_ratio": alt_ratio,
        "emails": emails,
        "phones": phones,
        "form_count": len(forms),
        "has_contact_form": has_contact_form,
        "agency_credit": agency_hit,
        "copyright_year": copyright_year,
        "has_analytics": any(m in text_lower for m in ANALYTICS_MARKERS),
        "has_consent_banner": any(m in text_lower for m in CONSENT_MARKERS),
        "modern_framework": any(m in text_lower for m in MODERN_FRAMEWORK_MARKERS),
        "jquery": "jquery" in text_lower,
        "resource_tags": len(soup.find_all(["script", "link", "img"])),
        "html_kb": round(len(html) / 1024, 1),
        "asset_url": asset_url,
        "mixed_content": mixed,
        "ttfb_s": round(elapsed, 2),
        "https": scheme == "https",
        "premium_slugs": [p for p in plugin_slugs if any(k in p for k in PREMIUM_SLUGS)],
        "legacy_markers": legacy,
    }


def probe_rest(s: requests.Session, root: str) -> dict[str, Any]:
    root = root.rstrip("/")
    attempts = [("pretty", root + "/wp-json/"), ("query", root + "/?rest_route=/")]
    saw_waf = saw_html = saw_404 = False

    for style, url in attempts:
        r = fetch(s, url, headers={"Accept": "application/json"})
        if r is None:
            continue
        if r.status_code in BLOCK_STATUSES:
            saw_waf = True
            continue
        if r.status_code == 404:
            saw_404 = True
            continue
        if r.status_code != 200:
            continue
        try:
            data = r.json()
        except ValueError:
            saw_html = True
            continue
        if not isinstance(data, dict) or "routes" not in data or "namespaces" not in data:
            saw_html = True
            continue

        routes = data.get("routes", {}) or {}
        namespaces = data.get("namespaces", []) or []
        namespaces_lower = " ".join(namespaces).lower()
        all_routes_lower = " ".join(routes.keys()).lower()
        full = namespaces_lower + " " + all_routes_lower
        woo_routes_lower = " ".join(k.lower() for k in routes if k.lower().startswith(("/wc", "/wc-")))
        commerce_haystack = namespaces_lower + " " + woo_routes_lower

        hard_ext = sorted(name for name, pat in HARD_COMMERCE_PATTERNS.items()
                          if re.search(pat, commerce_haystack))

        base = root + "/wp-json" if style == "pretty" else root + "/?rest_route="
        return {
            "status": "ok",
            "style": style,
            "base": base,
            "namespaces": namespaces,
            "namespace_count": len(namespaces),
            "route_count": len(routes),
            "woo_store": "wc/store" in full,
            "woo_legacy_api": "wc/v3" in full or "wc/v2" in full,
            "acf": "acf/v3" in full or "/acf/" in full,
            "menus": "wp/v2/menus" in full or "wp_navigation" in full,
            "blocks": "wp/v2/block-types" in full,
            "hard_extensions": hard_ext,
            "raw_index": r.text,
        }

    if saw_waf:
        status = "blocked"
    elif saw_html:
        status = "html_catchall"
    elif saw_404:
        status = "disabled"
    else:
        status = "unreachable"
    return {"status": status}


def rest_url(rest: dict[str, Any], route: str, params: dict[str, Any] | None = None) -> str:
    params = params or {}
    joiner = "?" if rest["style"] == "pretty" else "&"
    return rest["base"] + route + (joiner + urlencode(params) if params else "")


def rest_counts(s: requests.Session, rest: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}

    def meta(route: str) -> tuple[int | None, str | None]:
        r = fetch(s, rest_url(rest, route, {"per_page": 1, "orderby": "date", "order": "desc",
                                            "_fields": "date"}),
                  headers={"Accept": "application/json"})
        if r is None or r.status_code != 200:
            return None, None
        v = r.headers.get("X-WP-Total")
        total = int(v) if v and v.isdigit() else None
        newest = None
        try:
            arr = r.json()
            if isinstance(arr, list) and arr and isinstance(arr[0], dict):
                newest = arr[0].get("date")
        except ValueError:
            pass
        return total, newest

    post_total, post_newest = meta("/wp/v2/posts")
    page_total, page_newest = meta("/wp/v2/pages")
    out["posts"] = post_total
    out["pages"] = page_total

    dates = [d for d in (post_newest, page_newest) if d]
    if rest.get("woo_store"):
        prod_total, prod_newest = meta("/wc/store/v1/products")
        out["products"] = prod_total
        if prod_newest:
            dates.append(prod_newest)

    out["newest_date"] = max(dates) if dates else None
    return out


def asset_modified(s: requests.Session, url: str | None) -> str | None:
    if not url:
        return None
    r = fetch(s, url)
    return r.headers.get("Last-Modified") if r is not None else None


def parse_http_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(value)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def classify_block(status: int | None) -> str:
    if status is None:
        return "unreachable"
    if status in BLOCK_STATUSES:
        return "blocked"
    if status >= 500:
        return "unreachable"
    return "http_error"


def collect(target: str, ignore_robots: bool, business_live: bool | None = None) -> SiteReport:
    rep = SiteReport(target=target)
    s = session()
    root = normalize(target)
    rep.requested = registrable_domain(host_of(target))
    rep.domain = urlparse(root).hostname or target
    rep.registrable = registrable_domain(rep.domain)

    home, tls_broken = fetch_home(s, root)
    if home is None:
        rep.status = "unreachable"
        rep.rest_status = "unreachable"
        return rep
    if tls_broken:
        rep.flags.append("bad_tls")

    rep.http_status = home.status_code
    rep.url = home.url
    parsed = urlparse(home.url)
    # hostname, not netloc: a redirect may carry an explicit :443, which is not
    # part of the site's identity.
    rep.domain = parsed.hostname or rep.domain
    rep.registrable = registrable_domain(rep.domain)
    site_root = f"{parsed.scheme}://{parsed.netloc}"

    if home.status_code != 200:
        rep.status = classify_block(home.status_code)
        rep.rest_status = rep.status
        rep.wordpress = None
        rep.flags.append(f"http_{home.status_code}")
        return rep

    ctype = home.headers.get("Content-Type", "")
    if "html" not in ctype.lower():
        rep.status = "http_error"
        rep.rest_status = "not_html"
        rep.wordpress = None
        rep.flags.append(f"content_type:{ctype.split(';')[0] or 'unknown'}")
        return rep

    html = home.text
    if looks_like_challenge(html):
        rep.status = "blocked"
        rep.rest_status = "blocked"
        rep.wordpress = None
        rep.flags.append("challenge_page")
        return rep

    sigs = wordpress_signatures(html, home.headers.get("Link"))
    rep.wp_signatures = sigs
    facts = parse_homepage(html, home.url, home.elapsed.total_seconds(), parsed.scheme)
    facts["cert_error"] = tls_broken
    if tls_broken:
        facts["https"] = False
    facts["rest"] = {}
    facts["business_live"] = business_live
    facts.update(probe_responsive(s, facts.get("style_urls", []), facts.get("inline_media", 0)))
    rep.facts = facts

    if not sigs:
        rep.status = "not_wordpress"
        rep.rest_status = "not_wordpress"
        rep.wordpress = False
        return rep

    rep.wordpress = True
    if facts["generator"]:
        m = re.search(r"(\d+\.\d+)", facts["generator"])
        if m:
            rep.wp_version = m.group(1)

    rp, robots_status = robots_for(s, site_root)
    facts["robots_status"] = robots_status
    if rp is None and robots_status in BLOCK_STATUSES:
        rep.flags.append("robots_blocked")
    robots_disallow_rest = rp is not None and not rp.can_fetch(USER_AGENT, site_root + "/wp-json/")

    if robots_disallow_rest and not ignore_robots:
        rep.rest_status = "robots_disallowed"
        rep.flags.append("robots_disallows_wp_json")
    else:
        rest = probe_rest(s, site_root)
        rep.rest_status = rest.get("status", "unreachable")
        if rep.rest_status == "ok":
            rest.update(rest_counts(s, rest))
        facts["rest"] = rest

    facts["last_modified"] = asset_modified(s, facts.get("asset_url"))
    rep.status = "scored"
    return rep


def score(rep: SiteReport, meta: dict[str, Any] | None = None) -> None:
    """Score a WordPress site as a redesign prospect.

    Weighting is deliberate: defects and small-business fit decide the order,
    not size. Content volume used to be worth up to 12 points, which put city
    councils and 3,000-page shops above a solicitor with a broken certificate.
    """
    if rep.status == "blocked":
        rep.tier = "manual_review"
        return
    if rep.status == "not_wordpress":
        rep.tier = "not_wordpress"
        return
    if rep.status in ("unreachable", "http_error"):
        rep.tier = rep.status
        return

    f = rep.facts
    rest = f.get("rest", {})
    meta = meta or {}

    def add(axis: str, label: str, points: float, detail: str = "") -> None:
        rep.reasons.append(Reason(axis, label, points, detail))

    # --- defects: what is actually wrong with the site -------------------
    if f.get("cert_error"):
        add("Defects", "certificate_invalid", 12, "TLS certificate fails validation")
    elif not f.get("https"):
        add("Defects", "no_https", 12, "served over http")
    elif f.get("mixed_content"):
        add("Defects", "mixed_content", 6, "http assets on an https page")

    media = int(f.get("media_queries") or 0)
    fixed = int(f.get("fixed_widths") or 0)
    if not f.get("has_viewport"):
        add("Defects", "no_mobile_layout", 15, "no viewport meta tag")
    elif f.get("zoom_blocked"):
        add("Defects", "zoom_blocked", 6, "mobile zoom disabled")
    elif fixed and media <= 3:
        add("Defects", "fixed_width_layout", 12,
            f"{fixed} fixed widths, {media} media queries")
    elif fixed >= 3 and media <= 8:
        add("Defects", "weak_responsive", 6,
            f"{fixed} fixed widths, {media} media queries")

    legacy = [m for m in (f.get("legacy_markers") or [])
              if not m.startswith("page-builder:")]
    if legacy:
        add("Defects", "dated_front_end", min(len(legacy) * 3, 9), ", ".join(legacy))

    kb = f.get("html_kb") or 0
    if kb > 600:
        add("Defects", "page_weight", 5, f"{kb} KB HTML")
    elif kb > 300:
        add("Defects", "page_weight", 3, f"{kb} KB HTML")
    elif kb > 150:
        add("Defects", "page_weight", 1, f"{kb} KB HTML")

    ttfb = f.get("ttfb_s") or 0
    if ttfb > 2.0:
        add("Defects", "slow_server", 3, f"{ttfb}s to first byte")
    elif ttfb > 1.5:
        add("Defects", "slow_server", 2, f"{ttfb}s to first byte")

    a11y = []
    if not f.get("lang"):
        a11y.append("no lang attribute")
    alt_ratio = f.get("alt_ratio")
    if alt_ratio is not None and alt_ratio < 0.5:
        a11y.append(f"alt text on {round(alt_ratio * 100)}% of images")
    if a11y:
        add("Defects", "accessibility", 3, "; ".join(a11y))

    if rest.get("hard_extensions"):
        rep.flags.append("requires_human_scoping")

    # --- target fit: is this the kind of business worth emailing ---------
    identity = " ".join(filter(None, (
        str(meta.get("name") or ""),
        str(meta.get("category") or ""),
        str(f.get("title") or ""),
        str(f.get("meta_description") or ""),
        rep.requested or "",
        rep.domain or "",
    )))
    kws = extract_target_keywords(identity)
    if len(kws["strong"]) >= 2:
        add("Target fit", "small_business_category", 18, ", ".join(kws["strong"][:4]))
    elif kws["strong"]:
        add("Target fit", "small_business_category", 14, kws["strong"][0])
    elif kws["weak"]:
        add("Target fit", "possible_small_business", 7, ", ".join(kws["weak"][:3]))

    posts = rest.get("posts") or 0
    pages = rest.get("pages") or 0
    products = rest.get("products") or 0
    items = posts + pages + products
    if 5 <= items <= 200:
        add("Target fit", "small_site", 8, f"{items} items")
    elif items < 5:
        add("Target fit", "tiny_site", 4, f"{items} items")
    elif items <= 500:
        add("Target fit", "mid_size_site", 2, f"{items} items")
    else:
        add("Target fit", "large_site", -8, f"{items} items, harder sale")

    if products == 0:
        add("Target fit", "no_ecommerce", 4, "no product catalogue")
    elif products < 30:
        add("Target fit", "small_ecommerce", 4, f"{products} products")
    elif products <= 200:
        add("Target fit", "mid_ecommerce", 2, f"{products} products")

    if kws["enterprise"]:
        rep.penalties.append(Reason("Penalty", "enterprise_org", -15,
                                    ", ".join(kws["enterprise"][:3])))
        rep.flags.append("enterprise")

    # --- neglect: has anyone touched it --------------------------------
    content_age = None
    newest = parse_http_date(rest.get("newest_date")) or _iso_parse(rest.get("newest_date"))
    if newest is not None:
        content_age = (NOW.year - newest.year) * 12 + (NOW.month - newest.month)
        f["content_age_months"] = content_age

    cy = f.get("copyright_year")
    if cy and cy <= NOW.year - 2:
        add("Neglect", "stale_copyright", 5, f"footer copyright {cy}")
    elif cy == NOW.year - 1:
        add("Neglect", "copyright_lagging", 2, f"footer copyright {cy}")

    if content_age is not None and content_age > 60:
        add("Neglect", "no_recent_content", 5, f"no new content for {content_age} months")
    elif content_age is not None and content_age > 18:
        add("Neglect", "no_recent_content", 2, f"no new content for {content_age} months")

    lm = parse_http_date(f.get("last_modified"))
    if lm is not None:
        asset_age = (NOW.year - lm.year) * 12 + (NOW.month - lm.month)
        f["asset_age_months"] = asset_age
        if asset_age >= 18:
            add("Neglect", "stale_theme_assets", 3,
                f"theme files untouched {asset_age} months")

    if rep.wp_version:
        rel = WP_RELEASES.get(rep.wp_version)
        if rel is not None and months_since(*rel) > 18:
            add("Neglect", "stale_core", 2, f"WordPress {rep.wp_version}")

    live = f.get("business_live")
    if live is False:
        rep.penalties.append(Reason("Penalty", "possibly_dormant", -6,
                                    "business appears dormant"))
        rep.flags.append("possibly_dormant")

    # --- reachability ---------------------------------------------------
    if f.get("emails"):
        add("Reachability", "email", 7, f["emails"][0])
    if f.get("phones"):
        add("Reachability", "phone", 5, "public phone number")
    if f.get("has_contact_form"):
        add("Reachability", "contact_form", 3, "contact form")

    # --- penalties ------------------------------------------------------
    if f.get("modern_framework"):
        rep.penalties.append(Reason("Penalty", "modern_framework", -12,
                                    "React/Next/Nuxt front end, not a WordPress rebuild"))
    if f.get("agency_credit"):
        rep.penalties.append(Reason("Penalty", "agency_built", -4,
                                    f"footer mentions '{f['agency_credit']}'"))

    # A page builder rewriting its own CSS bumps Last-Modified constantly, so a
    # fresh asset alone is not a redesign. Require corroboration before
    # discounting a site.
    fresh = (
        f.get("asset_age_months") is not None and f["asset_age_months"] <= 6,
        content_age is not None and content_age <= 12,
        cy == NOW.year,
    )
    if sum(fresh) >= 2:
        rep.penalties.append(Reason("Penalty", "recent_redesign", -8,
                                    "recently refreshed theme, content and copyright"))

    # --- total ----------------------------------------------------------
    totals: dict[str, float] = {}
    for r in rep.reasons:
        totals[r.axis] = totals.get(r.axis, 0.0) + r.points
    capped = sum(max(0.0, min(totals.get(axis, 0.0), cap))
                 for axis, cap in CAPS.items())
    penalty = sum(p.points for p in rep.penalties)
    rep.score = round(max(0.0, min(100.0, capped + penalty)), 1)
    rep.facts["axis_raw"] = {k: round(v, 1) for k, v in totals.items()}

    for cut, label in TIERS:
        if rep.score >= cut:
            rep.tier = label
            break
    else:
        rep.tier = "discard"


def _iso_parse(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


STATUS_LABEL = {
    "blocked": "BLOCKED — refused automated requests; route to manual review",
    "unreachable": "UNREACHABLE — connection failed or server error",
    "http_error": "HTTP ERROR — non-200 response that is not a block",
    "not_wordpress": "NOT WORDPRESS — 200 page with no WordPress signatures",
}

def render(rep: SiteReport) -> str:
    lines = [f"=== {rep.url or rep.target} ==="]
    lines.append(f"status       {rep.status}  (http {rep.http_status})" if rep.http_status
                 else f"status       {rep.status}")
    lines.append(f"wordpress    {rep.wordpress}  version={rep.wp_version or '-'}"
                 + (f"  signatures={','.join(rep.wp_signatures)}" if rep.wp_signatures else ""))

    f = rep.facts
    rest = f.get("rest", {})
    if rest.get("status") == "ok":
        lines.append(f"rest         ok")
        lines.append(f"namespaces   {rest.get('namespace_count')}  routes={rest.get('route_count')}")
        lines.append(f"woo          store={rest.get('woo_store')}  legacy_api={rest.get('woo_legacy_api')}  acf={rest.get('acf')}  menus={rest.get('menus')}")
        lines.append(f"counts       posts={rest.get('posts')} pages={rest.get('pages')} products={rest.get('products')} newest={rest.get('newest_date')}")
    else:
        lines.append(f"rest         {rep.rest_status}")
    if f:
        if f.get("plugin_slugs"):
            lines.append(f"plugins      {len(f['plugin_slugs'])}: {', '.join(f['plugin_slugs'][:12])}")
        lines.append(f"theme        {', '.join(f.get('theme_slugs', [])) or '-'}")
        lines.append(f"page         {f.get('html_kb')} KB  resources={f.get('resource_tags')}  ttfb={f.get('ttfb_s')}s")

    if rep.status != "scored":
        lines.append("")
        lines.append(STATUS_LABEL.get(rep.status, rep.status))
        if rep.flags:
            lines.append(f"flags: {', '.join(sorted(set(rep.flags)))}")
        return "\n".join(lines)

    if rep.rest_status != "ok":
        lines.append(f"note         REST unusable ({rep.rest_status}); scored on frontend only")

    lines.append("")
    for axis in ["Defects", "Target fit", "Neglect", "Reachability"]:
        pts = [r for r in rep.reasons if r.axis == axis]
        lines.append(f"{axis:<16} {sum(r.points for r in pts):>5.1f}/{CAPS[axis]}")
        for r in pts:
            d = f"  ({r.detail})" if r.detail else ""
            lines.append(f"    {r.label:<22} {r.points:>4.1f}{d}")
    if rep.penalties:
        lines.append(f"{'Penalties':<16} {sum(p.points for p in rep.penalties):>5.1f}")
        for p in rep.penalties:
            lines.append(f"    {p.label:<22} {p.points:>4.1f}  ({p.detail})")

    lines.append("")
    lines.append(f"SCORE {rep.score}  TIER {rep.tier}")
    if rep.flags:
        lines.append(f"flags: {', '.join(sorted(set(rep.flags)))}")
    return "\n".join(lines)


def build_row(rep: SiteReport) -> dict[str, Any]:
    rest = dict(rep.facts.get("rest", {}))
    raw_index = rest.pop("raw_index", None)
    facts = dict(rep.facts)
    facts["rest"] = rest
    return {
        "registrable": rep.registrable,
        "requested": rep.requested,
        "host": rep.domain,
        "url": rep.url,
        "status": rep.status,
        "http_status": rep.http_status,
        "wordpress": None if rep.wordpress is None else int(rep.wordpress),
        "wp_signatures": rep.wp_signatures,
        "wp_version": rep.wp_version,
        "rest_status": rep.rest_status,
        "woo_store": int(bool(rest.get("woo_store"))),
        "route_count": rest.get("route_count"),
        "namespace_count": rest.get("namespace_count"),
        "post_count": rest.get("posts"),
        "page_count": rest.get("pages"),
        "product_count": rest.get("products"),
        "score": rep.score,
        "tier": rep.tier,
        "flags": rep.flags,
        "score_version": SCORE_VERSION,
        "facts": facts,
        "reasons": [r.__dict__ for r in rep.reasons],
        "penalties": [p.__dict__ for p in rep.penalties],
        "raw_index": raw_index,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Score WordPress/WooCommerce sites as redesign prospects.")
    ap.add_argument("domains", nargs="*", help="domains or URLs")
    ap.add_argument("-f", "--file", help="file with one domain per line")
    ap.add_argument("--json", action="store_true", help="emit full JSON array to stdout")
    ap.add_argument("--jsonl", help="append one JSON object per WordPress site to this file")
    ap.add_argument("--db", default="data/leads.db", help="SQLite path (default data/leads.db)")
    ap.add_argument("--no-db", action="store_true", help="do not write to SQLite")
    ap.add_argument("--force", action="store_true", help="re-score domains already in the database")
    ap.add_argument("--retry-failed", action="store_true",
                    help="also retry domains previously blocked/unreachable")
    ap.add_argument("--batch", type=int, default=0, metavar="N",
                    help="score at most N not-yet-checked domains this run (0 = all)")
    ap.add_argument("--workers", type=int, default=1, metavar="N",
                    help="fetch N sites concurrently (default 1)")
    ap.add_argument("--quiet", action="store_true", help="print only the summary, not each site")
    ap.add_argument("--ignore-robots", action="store_true", help="probe /wp-json/ even if robots.txt disallows")
    ap.add_argument("--candidates", help="discover.py candidates.json for business name/category")
    args = ap.parse_args()

    candidates: dict[str, Any] = {}
    if args.candidates and os.path.exists(args.candidates):
        with open(args.candidates, encoding="utf-8") as fh:
            candidates = json.load(fh)

    inputs = list(args.domains)
    if args.file:
        with open(args.file, encoding="utf-8") as fh:
            inputs += [ln.strip() for ln in fh if ln.strip() and not ln.startswith("#")]
    if not inputs:
        ap.print_help()
        return 1

    unique: set[str] = set()
    deduped: list[str] = []
    duplicates = 0
    for target in inputs:
        reg = registrable_domain(host_of(target))
        if reg in unique:
            duplicates += 1
            continue
        unique.add(reg)
        deduped.append(target)

    con = None if args.no_db else store.connect(args.db)
    known = store.known_domains(con) if con else set()
    retryable = store.retryable_domains(con) if (con and args.retry_failed) else set()

    targets: list[str] = []
    already = 0
    for target in deduped:
        reg = registrable_domain(host_of(target))
        if con and not args.force:
            if reg in known and reg not in retryable:
                already += 1
                continue
        targets.append(target)

    deferred = 0
    if args.batch and len(targets) > args.batch:
        deferred = len(targets) - args.batch
        targets = targets[:args.batch]

    def run_one(target: str) -> SiteReport:
        try:
            rep = collect(target, args.ignore_robots)
            meta = candidates.get(rep.requested or "", {})
            score(rep, meta)
            return rep
        except Exception as exc:  # never let one bad site kill the batch
            rep = SiteReport(target=target)
            rep.requested = registrable_domain(host_of(target))
            rep.domain = host_of(target)
            rep.registrable = registrable_domain(rep.domain)
            rep.status = "unreachable"
            rep.flags.append(f"error:{type(exc).__name__}")
            return rep

    reports: list[SiteReport] = []
    stored = 0

    def handle(rep: SiteReport, index: int, total: int) -> None:
        nonlocal stored
        reports.append(rep)

        # Only WordPress sites get a full row (facts, reasons, REST index).
        if con:
            store.mark_seen(con, rep.registrable, rep.status, rep.tier, rep.score,
                            rep.wordpress, requested=rep.requested)
        if rep.wordpress:
            row = build_row(rep)
            raw_index = row.pop("raw_index", None)
            if con:
                store.save(con, row, raw_index.encode("utf-8") if isinstance(raw_index, str) else raw_index)
            if args.jsonl:
                store.append_jsonl(args.jsonl, row)
            stored += 1

        if args.workers > 1 and not args.quiet:
            label = rep.tier if rep.status == "scored" else rep.status
            pts = f"{rep.score:.0f}" if rep.score is not None else "-"
            print(f"[{index}/{total}] {rep.domain}  {label}  {pts}", file=sys.stderr)

        if not args.json and not args.quiet:
            print(render(rep))
            print()

    if args.workers > 1:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = [pool.submit(run_one, t) for t in targets]
            for i, fut in enumerate(futures, 1):
                handle(fut.result(), i, len(targets))
    else:
        for i, target in enumerate(targets, 1):
            handle(run_one(target), i, len(targets))

    if args.json:
        print(json.dumps([r.to_dict() for r in reports], indent=2, default=str))
    else:
        counts: dict[str, int] = {}
        for rep in reports:
            key = rep.tier if rep.status == "scored" else rep.status
            counts[key] = counts.get(key, 0) + 1
        summary = "  ".join(f"{k}:{v}" for k, v in sorted(counts.items())) or "nothing to do"
        line = f"summary: {len(reports)} checked, {stored} wordpress"
        if already:
            line += f", {already} already checked"
        if duplicates:
            line += f", {duplicates} duplicate input"
        if deferred:
            line += f", {deferred} deferred to next batch"
        if args.retry_failed and retryable:
            line += f", {len(retryable)} retry eligible"
        if not args.no_db:
            line += f"  |  db={args.db}"
        print(f"{line}  |  {summary}")

    if con:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
