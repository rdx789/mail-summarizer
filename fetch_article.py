"""
Standalone utility — fetch article text from a URL with a paywall-bypass
fallback chain. No project-specific constants; import directly.
"""
import json
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; ArticleFetcher/1.0)'}

BROWSER_USER_AGENT = (
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36'
)

# Cookies exported from a logged-in medium.com session (plain JSON, e.g. via
# a browser cookie-export extension — NOT the encrypted "Cookie Manager"
# format). See test_first_email.py / the mail-summarizer README for how to
# export these. Gitignored — this is a live session credential.
MEDIUM_COOKIES_PATH = 'medium_cookies.json'

_SAME_SITE_MAP = {
    'no_restriction': 'None', 'lax': 'Lax', 'strict': 'Strict', 'unspecified': 'Lax',
}

# Medium sits behind Cloudflare and hard-blocks plain requests/curl fetches
# (confirmed: even the bare homepage 403s, even with a browser User-Agent
# and valid session cookies attached via HTTP headers). Only a real browser
# engine gets past it. Playwright's sync API is not thread-safe across
# threads, so all Medium fetches funnel through one lazily-launched shared
# browser guarded by this lock — Medium fetches are serialized while
# fetches for every other domain stay fully parallel across ARTICLE_WORKERS.
_medium_lock = threading.Lock()
_medium_playwright = None
_medium_browser = None

PAYWALL_PATTERNS = [
    r'subscribe to \S.*?to (read|unlock|access|continue)',
    r'subscribe to .{0,40} to unlock',
    r'this post is for paid subscribers',
    r'unlock (the full|this|the rest)',
    r'already a paid subscriber',
    r'become a (paid |paying )?subscriber',
    r'become a (paid )?member',
    r'sign in to read (the rest|more|the full)',
    r'get the full story',
    r'read the full (post|article) with a subscription',
    r'continue reading with a',
    r'create a free account to (read|continue)',
    r'upgrade to paid',
    r'subscriber.only content',
    r'(get|have) access to this post',
]


def detect_paywall(text):
    """Heuristic: does the tail of this text look like a paywall wall rather
    than real article content? Checks the tail (not the whole text) since
    paywall notices are almost always appended at the end."""
    tail = text[-1000:].lower()
    return any(re.search(p, tail) for p in PAYWALL_PATTERNS)


def html_to_text(html, cap=10000):
    soup = BeautifulSoup(html, 'html.parser')
    for tag in soup(['script', 'style', 'nav', 'footer', 'header']):
        tag.decompose()
    return re.sub(r'\s+', ' ', soup.get_text(separator=' ')).strip()[:cap]


def fetch_article_text(url, timeout=15):
    """Direct fetch, no fallback chain — use when you already know the URL
    is freely accessible."""
    r = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
    r.raise_for_status()
    return html_to_text(r.text)


def _try_archive_ph(url, timeout=15):
    r = requests.get(f"https://archive.ph/newest/{url}", headers=HEADERS,
                      timeout=timeout, allow_redirects=True)
    r.raise_for_status()
    return html_to_text(r.text)


def _try_substack_rss(url, timeout=15):
    """Only applicable to <subdomain>.substack.com/p/<slug> URLs — returns
    None (not an error) for anything else, so the caller can skip to the
    next strategy."""
    m = re.match(r'https?://([^/]+\.substack\.com)/p/([^/?#]+)', url)
    if not m:
        return None
    domain, slug = m.group(1), m.group(2)
    r = requests.get(f"https://{domain}/feed", headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    ns = {'content': 'http://purl.org/rss/1.0/modules/content/'}
    root = ET.fromstring(r.content)
    for item in root.iter('item'):
        link_el = item.find('link')
        link_text = (link_el.text or '') if link_el is not None else ''
        if slug not in link_text:
            continue
        for tag in ('content:encoded', 'description'):
            el = item.find(tag, ns) if ':' in tag else item.find(tag)
            if el is not None and el.text:
                text = html_to_text(el.text)
                if text:
                    return text
    return None


def _try_12ft(url, timeout=15):
    r = requests.get(f"https://12ft.io/proxy?q={url}", headers=HEADERS,
                      timeout=timeout, allow_redirects=True)
    r.raise_for_status()
    return html_to_text(r.text)


def _is_medium_url(url):
    return urlparse(url).netloc.endswith('medium.com')


def _load_medium_cookies():
    """Returns Playwright-format cookies, or None if the file is missing/
    invalid — callers should treat that as "skip this strategy", not an
    error, since Medium fetching should degrade to the rest of the fallback
    chain rather than crash the run."""
    if not os.path.exists(MEDIUM_COOKIES_PATH):
        return None
    try:
        with open(MEDIUM_COOKIES_PATH, encoding='utf-8') as f:
            data = json.load(f)
        raw_cookies = data['cookies']
        cookies = []
        for c in raw_cookies:
            cookie = {
                'name': c['name'], 'value': c['value'],
                'domain': c['domain'], 'path': c['path'],
                'httpOnly': c.get('httpOnly', False),
                'secure': c.get('secure', False),
                'sameSite': _SAME_SITE_MAP.get(c.get('sameSite'), 'Lax'),
            }
            if not c.get('session') and 'expirationDate' in c:
                cookie['expires'] = c['expirationDate']
            cookies.append(cookie)
        return cookies
    except (json.JSONDecodeError, OSError, KeyError):
        return None


def _get_medium_browser():
    """Lazily launches one shared headless Chromium instance, reused across
    the run (launching a fresh browser per article would be far slower).
    Must only be called while holding _medium_lock."""
    global _medium_playwright, _medium_browser
    if _medium_browser is None:
        from playwright.sync_api import sync_playwright
        _medium_playwright = sync_playwright().start()
        _medium_browser = _medium_playwright.chromium.launch(
            headless=True, args=['--disable-blink-features=AutomationControlled'],
        )
    return _medium_browser


def _try_medium_playwright(url, timeout=20):
    """Fetch a Medium article with a real (headless) Chromium loaded with
    the user's Medium session cookies — the only strategy that gets past
    Medium's Cloudflare block, which rejects plain requests/curl fetches
    outright (confirmed even with a browser User-Agent and valid cookies
    sent as raw HTTP headers)."""
    cookies = _load_medium_cookies()
    if cookies is None:
        return None  # not configured — let the caller fall through

    with _medium_lock:
        browser = _get_medium_browser()
        context = browser.new_context(
            user_agent=BROWSER_USER_AGENT, viewport={'width': 1280, 'height': 800},
        )
        try:
            context.add_cookies(cookies)
            page = context.new_page()
            page.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            page.goto(url, wait_until='domcontentloaded', timeout=timeout * 1000)
            page.wait_for_timeout(3000)  # let client-rendered content settle
            text = page.inner_text('body')
        finally:
            context.close()
    return re.sub(r'\s+', ' ', text).strip()


def fetch_with_fallbacks(url, fallback_text="", verbose=True):
    """
    Fetch article text, trying a direct fetch then archive.ph / Substack RSS
    / 12ft.io in order if the direct fetch is paywalled or fails. For
    medium.com URLs, tries the authenticated-Chromium strategy first and
    skips the plain direct fetch, since Medium's Cloudflare block rejects
    every plain requests/curl fetch outright regardless of headers.

    Returns (content, source_label, hit_paywall):
      - source_label is 'direct', 'medium-playwright', 'archive.ph',
        'Substack RSS', '12ft.io', or 'fallback' (all strategies failed —
        content is fallback_text).
      - hit_paywall=True means every strategy failed or still looked
        paywalled — don't present `content` as the complete article.
    """
    def log(msg):
        if verbose:
            print(msg)

    hit_paywall = False

    if _is_medium_url(url):
        log(f"Fetching (Medium): {url}")
        try:
            text = _try_medium_playwright(url)
            if text is None:
                log("  [medium-playwright] not configured (no medium_cookies.json).")
            elif not detect_paywall(text):
                log(f"  [medium-playwright] -> {len(text)} chars")
                return text, 'medium-playwright', False
            else:
                log("  [medium-playwright] still looks paywalled.")
                hit_paywall = True
        except Exception as e:
            log(f"  [medium-playwright] failed: {e}")

        for label, fn in [('archive.ph', _try_archive_ph), ('12ft.io', _try_12ft)]:
            try:
                text = fn(url)
                log(f"  [{label}] -> {len(text)} chars")
                if not detect_paywall(text):
                    return text, label, False
                log(f"  Still paywalled via {label}.")
                hit_paywall = True
            except Exception as e:
                log(f"  [{label}] failed: {e}")

        log("  All strategies exhausted.")
        return fallback_text, 'fallback', hit_paywall

    log(f"Fetching: {url}")
    try:
        t0 = time.perf_counter()
        text = fetch_article_text(url)
        log(f"  -> {len(text)} chars ({time.perf_counter()-t0:.1f}s)")
        if not detect_paywall(text):
            return text, 'direct', False
        log("  Paywall detected.")
        hit_paywall = True
    except Exception as e:
        log(f"  -> failed: {e}")

    for label, fn in [('archive.ph', _try_archive_ph),
                       ('Substack RSS', _try_substack_rss),
                       ('12ft.io', _try_12ft)]:
        try:
            text = fn(url)
            if text is None:
                log(f"  [{label}] not applicable.")
                continue
            log(f"  [{label}] -> {len(text)} chars")
            if not detect_paywall(text):
                return text, label, False
            log(f"  Still paywalled via {label}.")
        except Exception as e:
            log(f"  [{label}] failed: {e}")

    log("  All strategies exhausted.")
    return fallback_text, 'fallback', hit_paywall


if __name__ == '__main__':
    import sys
    if len(sys.argv) != 2:
        print("Usage: python fetch_article.py <url>")
        raise SystemExit(1)
    content, source, hit_paywall = fetch_with_fallbacks(sys.argv[1])
    print(f"\n--- source: {source}  paywall: {hit_paywall} ---")
    print(content[:1000])
