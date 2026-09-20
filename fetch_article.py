"""
Standalone utility — fetch article text from a URL with a paywall-bypass
fallback chain. No project-specific constants; import directly.
"""
import re
import time
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup

HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; ArticleFetcher/1.0)'}

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


def fetch_with_fallbacks(url, fallback_text="", verbose=True):
    """
    Fetch article text, trying a direct fetch then archive.ph / Substack RSS
    / 12ft.io in order if the direct fetch is paywalled or fails.

    Returns (content, source_label, hit_paywall):
      - source_label is 'direct', 'archive.ph', 'Substack RSS', '12ft.io',
        or 'fallback' (all strategies failed — content is fallback_text).
      - hit_paywall=True means every strategy failed or still looked
        paywalled — don't present `content` as the complete article.
    """
    def log(msg):
        if verbose:
            print(msg)

    hit_paywall = False

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
