"""
Mail summarizer — same behavior as ../mail/main.py, but built on top of three
extracted skills instead of inline implementations:
  - gmail_auth.py    (gmail-oauth-bootstrap skill)
  - fetch_article.py (paywall-article-fetcher skill)
  - local_llm.py     (local-llm-backend skill, LLM_BACKEND=openai_compatible)
"""
import json
import os
import re
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from urllib.parse import urlparse, parse_qs

from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

from gmail_auth import get_gmail_service          # noqa: E402  (needs load_dotenv() first)
from fetch_article import fetch_with_fallbacks    # noqa: E402
from local_llm import generate, strip_thinking, strip_preamble  # noqa: E402

SUMMARIES_DIR       = os.path.expanduser('~/Documents/email-summarized-with-skills')
LAST_RUN_PATH       = 'last_run.json'
SUMMARIZED_LABEL    = 'summarized'

# Cap how many article links we summarize per newsletter email.
MAX_ARTICLES_PER_EMAIL = 10

# Fetch + summarize articles within an email concurrently. llama-server's
# default of 4 slots is the measured sweet spot on this hardware — 5 slots
# was actually *slower* wall-clock (memory-bandwidth contention outweighed
# the extra parallelism), so don't bump this without re-benchmarking.
ARTICLE_WORKERS = 4

EXCLUDED_SENDERS_PATH = 'excluded_senders.json'

# Cache article fetch+summarize results by URL, scoped to the current
# calendar day — run_mail_summarizer.sh fires every 4 hours, so the same
# article link can show up in two different digests processed hours apart
# on the same day, and this skips both the network fetch and the LLM call
# entirely on a repeat. Reset once the day rolls over: by the next day a
# given article URL is very unlikely to reappear, so there's no value in
# keeping entries around indefinitely — just an ever-growing file. Only
# successful results are cached; a failed/errored summarize() is retried
# next time, not stuck.
ARTICLE_CACHE_PATH = 'article_cache.json'
_article_cache_lock = threading.Lock()


def load_article_cache():
    if not os.path.exists(ARTICLE_CACHE_PATH):
        return {}
    try:
        with open(ARTICLE_CACHE_PATH, encoding='utf-8') as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}

    today = datetime.now().strftime('%Y-%m-%d')
    if data.get('date') != today:
        return {}  # new day — start fresh, don't carry stale entries forward
    return data.get('urls', {})


def save_article_cache(cache):
    data = {'date': datetime.now().strftime('%Y-%m-%d'), 'urls': cache}
    with open(ARTICLE_CACHE_PATH, 'w', encoding='utf-8') as f:
        json.dump(data, f)


def load_excluded_senders():
    if not os.path.exists(EXCLUDED_SENDERS_PATH):
        return set()
    with open(EXCLUDED_SENDERS_PATH, encoding='utf-8') as f:
        data = json.load(f)
    return {s.strip() for s in data if isinstance(s, str) and s.strip()}


_SKIP_TEXT = re.compile(
    r'upgrade to paid|subscribe (here|now|to)|unsubscribe|'
    r'sign (up|in)|log ?in|read in app|open in app|'
    r'view in browser|manage (subscription|preferences)|'
    r'forward(ed)? this email|share|give a gift',
    re.IGNORECASE,
)
_SKIP_URL = re.compile(
    r'/subscribe|/unsubscribe|/login|/signup|/checkout|'
    r'utm_campaign=email.checkout',
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Cursor
# ---------------------------------------------------------------------------

def load_cursor():
    if os.path.exists(LAST_RUN_PATH):
        try:
            with open(LAST_RUN_PATH) as f:
                return json.load(f).get('last_internal_date_ms')
        except Exception:
            pass
    return None


def save_cursor(internal_date_ms):
    with open(LAST_RUN_PATH, 'w') as f:
        json.dump({'last_internal_date_ms': internal_date_ms}, f)


# ---------------------------------------------------------------------------
# Gmail label helpers (auth itself comes from gmail_auth.get_gmail_service)
# ---------------------------------------------------------------------------

def get_or_create_label(service, name):
    labels = service.users().labels().list(userId='me').execute().get('labels', [])
    for label in labels:
        if label['name'].lower() == name.lower():
            return label['id']
    result = service.users().labels().create(
        userId='me',
        body={
            'name': name,
            'labelListVisibility':   'labelShow',
            'messageListVisibility': 'show',
        },
    ).execute()
    print(f"  Created Gmail label '{name}'")
    return result['id']


def apply_label(service, message_id, label_id):
    try:
        service.users().messages().modify(
            userId='me',
            id=message_id,
            body={'addLabelIds': [label_id]},
        ).execute()
    except Exception as e:
        print(f"  ⚠  Could not apply label to {message_id}: {e}")


# ---------------------------------------------------------------------------
# Email parsing
# ---------------------------------------------------------------------------

def _clean_sender_name(raw):
    name = re.sub(r'\s*<[^>]+>', '', raw).strip().strip('"').strip("'")
    return name if name else raw


def sender_slug(name):
    ascii_only = re.sub(r'[^\x00-\x7F]+', '', name)
    slug = re.sub(r'[^\w\s-]', '', ascii_only.lower())
    slug = re.sub(r'[\s_]+', '-', slug).strip('-')
    if not slug:
        slug = f'sender-{abs(hash(name)) % 99999}'
    return slug[:50]


def parse_and_clean_email_body(gmail_service, user_id, message_id):
    import base64

    message = gmail_service.users().messages().get(
        userId=user_id, id=message_id, format='full'
    ).execute()

    payload  = message.get('payload', {})
    headers  = payload.get('headers', [])
    subject  = next((h['value'] for h in headers if h['name'].lower() == 'subject'), '(no subject)')
    sender   = next((h['value'] for h in headers if h['name'].lower() == 'from'), '(unknown sender)')
    is_bulk  = any(h['name'].lower() == 'list-unsubscribe' for h in headers)

    internal_date_ms = int(message.get('internalDate', 0))
    date_str = datetime.fromtimestamp(internal_date_ms / 1000).strftime('%Y-%m-%d')

    def get_body_content(part):
        data = part.get('body', {}).get('data', '')
        return base64.urlsafe_b64decode(data).decode('utf-8', errors='ignore') if data else ''

    raw_text = raw_html = ''

    def walk_parts(part):
        nonlocal raw_text, raw_html
        mt = part.get('mimeType', '')
        if mt == 'text/plain' and not raw_text:
            raw_text = get_body_content(part)
        elif mt == 'text/html' and not raw_html:
            raw_html = get_body_content(part)
        for sub in part.get('parts', []):
            walk_parts(sub)

    walk_parts(payload)

    content = raw_text
    if not content and raw_html:
        soup = BeautifulSoup(raw_html, 'html.parser')
        for tag in soup(['script', 'style']):
            tag.decompose()
        content = soup.get_text(separator=' ')

    cleaned = strip_tracking_links(truncate_reply_chains(content) if content else '')
    story_links = extract_story_links(raw_html) if raw_html and is_bulk else []

    return {
        'message_id':       message_id,
        'subject':          subject,
        'sender':           sender,
        'sender_name':      _clean_sender_name(sender),
        'body':             cleaned,
        'story_links':      story_links,
        'date':             date_str,
        'is_bulk':          is_bulk,
        'internal_date_ms': internal_date_ms,
        'raw_html':         raw_html,
    }


# ---------------------------------------------------------------------------
# Link extraction
# ---------------------------------------------------------------------------

def extract_story_links(html):
    soup = BeautifulSoup(html, 'html.parser')
    seen, links = set(), []
    for a in soup.find_all('a', href=True):
        href = str(a['href'])
        text = a.get_text(strip=True)
        if len(text) < 15:
            continue
        if _SKIP_TEXT.search(text) or _SKIP_URL.search(href):
            continue
        path = re.sub(r'^https?://[^/]+', '', href).split('?')[0]
        if re.match(r'^/@[^/]+$', path) or path.count('/') <= 1:
            continue
        base = href.split('?')[0]
        if base in seen:
            continue
        seen.add(base)
        links.append({'title': text, 'url': base})
    return links


def extract_paywall_article_urls(raw_html):
    soup = BeautifulSoup(raw_html, 'html.parser')
    found, seen = [], set()
    for a in soup.find_all('a', href=True):
        href = str(a['href'])
        if 'utm_campaign=email-checkout' not in href and 'utm_source=paywall' not in href:
            continue
        params  = parse_qs(urlparse(href).query)
        next_url = params.get('next', [None])[0]
        if not next_url or not next_url.startswith('http'):
            continue
        if re.search(r'/subscribe', next_url, re.IGNORECASE):
            continue
        base = next_url.split('?')[0]
        if base in seen:
            continue
        seen.add(base)
        found.append({'title': a.get_text(strip=True) or 'Article', 'url': base})
    return found


# ---------------------------------------------------------------------------
# Summarization (via local_llm skill)
# ---------------------------------------------------------------------------

def load_summary_prompt(path='post-prompt.md'):
    with open(path, encoding='utf-8') as f:
        return f.read().strip()


def summarize(content, system_prompt):
    # No "/no_think" prefix: it's ignored by newer thinking models (e.g.
    # Qwen3.6) — thinking is disabled properly via local_llm.py's
    # chat_template_kwargs instead.
    raw = generate(content, system_prompt=system_prompt)
    return strip_preamble(strip_thinking(raw))


def process_link(link, body, summary_prompt, cache):
    url = link['url']
    with _article_cache_lock:
        cached = cache.get(url)
    if cached is not None:
        return cached['source'], cached['hit_paywall'], cached['summary']

    content, source, hit_paywall = fetch_with_fallbacks(link['url'], body)
    try:
        summary = summarize(content, summary_prompt)
    except Exception as exc:
        # Don't cache — a transient network/LLM failure should be retried
        # on the next run, not permanently stuck as the cached result.
        return source, hit_paywall, f"*(could not summarize: {exc})*"

    with _article_cache_lock:
        cache[url] = {'source': source, 'hit_paywall': hit_paywall, 'summary': summary}
    return source, hit_paywall, summary


def summarize_body(body, summary_prompt):
    try:
        return summarize(body, summary_prompt)
    except Exception as exc:
        return f"*(could not summarize: {exc})*"


# ---------------------------------------------------------------------------
# Text cleaning
# ---------------------------------------------------------------------------

def strip_tracking_links(text):
    text = re.sub(r'\s*\(https?://\S+\)', '', text)
    return re.sub(r'\n\s*\n+', '\n\n', text).strip()


def truncate_reply_chains(text):
    patterns = [
        r'-\s*Original Message\s*-', r'From:\s*.*',
        r'On\s+.*?\s+wrote:', r'________________________________', r'^\s*>+.*',
    ]
    lines = []
    for line in text.splitlines():
        if any(re.search(p, line, re.IGNORECASE) for p in patterns):
            break
        if not line.strip().startswith('>'):
            lines.append(line)
    return re.sub(r'\n\s*\n+', '\n\n', '\n'.join(lines)).strip()


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def _day_label(date_str):
    return datetime.strptime(date_str, '%Y-%m-%d').strftime('%A, %B %-d %Y')


def write_day_index(day_dir, date_str, sender_rows, body_only_rows,
                    excluded_rows, skipped_rows):
    path = os.path.join(day_dir, '_index.md')
    with open(path, 'w', encoding='utf-8') as f:
        f.write(f"# {_day_label(date_str)}\n\n")

        if sender_rows:
            f.write("## Newsletters with articles\n\n")
            f.write("| Sender | Emails | Articles summarized |\n")
            f.write("|---|---|---|\n")
            for row in sender_rows:
                of_total = f"{row['summarized']} of {row['total']}" if row['total'] else '—'
                f.write(f"| {row['sender']} | {row['emails']} | {of_total} |\n")
            f.write("\n")

        if body_only_rows:
            f.write("## Newsletters — email body summarized (no article links)\n\n")
            for row in body_only_rows:
                f.write(f"- **{row['sender']}** · {row['subject']}\n")
            f.write("\n")

        if excluded_rows:
            f.write("## Excluded — links listed, not summarized\n\n")
            f.write("| Sender | Emails | Links found |\n")
            f.write("|---|---|---|\n")
            for row in excluded_rows:
                f.write(f"| {row['sender']} | {row['emails']} | {row['links']} |\n")
            f.write("\n")

        if skipped_rows:
            f.write("## Transactional / not processed\n\n")
            for row in skipped_rows:
                f.write(f"- {row['sender']} · {row['subject']}\n")
            f.write("\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    service          = get_gmail_service()
    summary_prompt   = load_summary_prompt()
    label_id         = get_or_create_label(service, SUMMARIZED_LABEL)
    article_cache    = load_article_cache()
    EXCLUDED_SENDERS = load_excluded_senders()
    if EXCLUDED_SENDERS:
        print(f"Excluded senders ({len(EXCLUDED_SENDERS)}): {', '.join(sorted(EXCLUDED_SENDERS))}")

    cursor_ms = load_cursor()
    if cursor_ms:
        after_ts = int(cursor_ms // 1000)
        print(f"Fetching emails after {datetime.fromtimestamp(after_ts).strftime('%Y-%m-%d %H:%M:%S')}")
    else:
        after_ts = int((datetime.now() - timedelta(days=1)).timestamp())
        print("No cursor — fetching last 1 days.")

    results  = service.users().messages().list(userId='me', q=f'in:inbox after:{after_ts}').execute()
    messages = results.get('messages', [])
    if not messages:
        print("No new emails.")
        exit(0)

    all_emails = []
    for i, msg in enumerate(messages, 1):
        print(f"\rParsing {i}/{len(messages)}...", end='', flush=True)
        parsed = parse_and_clean_email_body(service, 'me', msg['id'])
        if cursor_ms and parsed['internal_date_ms'] <= cursor_ms:
            continue
        all_emails.append(parsed)
    print(f"\rParsed {len(all_emails)} new emails.        ")

    all_emails.sort(key=lambda e: e['internal_date_ms'])

    print(f"\nFound {len(all_emails)} new email(s):")
    for i, em in enumerate(all_emails, 1):
        bulk_tag = " [newsletter]" if em['is_bulk'] else ''
        link_tag = f" [{len(em['story_links'])} links]" if em['story_links'] else ''
        print(f"  {i:3d}. {em['sender_name'][:35]:<35} {em['subject'][:45]}{bulk_tag}{link_tag}")
    print('-' * 80)

    os.makedirs(SUMMARIES_DIR, exist_ok=True)

    by_day = defaultdict(list)
    for em in all_emails:
        by_day[em['date']].append(em)

    newest_date_ms = cursor_ms or 0

    # One pool for the entire run (not one per email) — keeps all
    # ARTICLE_WORKERS slots busy across email/sender/day boundaries instead
    # of leaving slots idle whenever an email has fewer links than workers.
    article_pool = ThreadPoolExecutor(max_workers=ARTICLE_WORKERS)

    for date_str in sorted(by_day.keys()):
        day_emails = by_day[date_str]
        day_dir    = os.path.join(SUMMARIES_DIR, date_str)
        os.makedirs(day_dir, exist_ok=True)

        print(f"\n{'=' * 70}")
        print(f"  {_day_label(date_str)}  —  {len(day_emails)} emails")
        print(f"{'=' * 70}")

        newsletters   = [e for e in day_emails if e['is_bulk']]
        transactional = [e for e in day_emails if not e['is_bulk']]

        by_sender = defaultdict(list)
        for em in newsletters:
            by_sender[em['sender_name']].append(em)

        sender_rows    = []
        body_only_rows = []
        excluded_rows  = []
        skipped_rows   = [{'sender': e['sender_name'], 'subject': e['subject']}
                          for e in transactional]

        _excluded_lower = {s.lower() for s in EXCLUDED_SENDERS}

        # Phase A — submit ALL non-excluded senders' fetch+summarize work to
        # the shared pool up front, across the whole day, before writing
        # anything. This is what actually keeps all ARTICLE_WORKERS slots
        # busy: a sender with only 1-2 links no longer leaves slots idle
        # while a later sender's work sits unsubmitted and waiting — every
        # article across every sender is in the queue from the start.
        sender_plans = {}  # sender_name -> [(em, all_links, capped, futures_or_future), ...]
        for sender_name, sender_emails in by_sender.items():
            if sender_name.lower() in _excluded_lower:
                continue

            email_plans = []
            for em in sender_emails:
                all_links = list(em['story_links'])
                seen_urls = {l['url'] for l in all_links}
                for pl in extract_paywall_article_urls(em['raw_html']):
                    if pl['url'] not in seen_urls:
                        all_links.append(pl)
                        seen_urls.add(pl['url'])

                capped = all_links[:MAX_ARTICLES_PER_EMAIL]

                if capped:
                    print(f"\n  {sender_name} · {em['subject'][:50]}")
                    for link in capped:
                        print(f"    → {link['title'][:60]}")
                    futures = [
                        article_pool.submit(process_link, link, em['body'], summary_prompt, article_cache)
                        for link in capped
                    ]
                    email_plans.append((em, all_links, capped, futures))
                else:
                    print(f"\n  {sender_name} · {em['subject'][:50]}  [body only]")
                    future = article_pool.submit(summarize_body, em['body'], summary_prompt)
                    email_plans.append((em, None, None, future))
            sender_plans[sender_name] = email_plans

        # Phase B — write output in original order, one sender at a time.
        # Many futures will already be done by the time we get here, since
        # the pool has been working through the whole day's queue the whole
        # time we were submitting in Phase A.
        for sender_name, sender_emails in by_sender.items():
            slug     = sender_slug(sender_name)
            out_path = os.path.join(day_dir, f'{slug}.md')

            if sender_name.lower() in _excluded_lower:
                print(f"\n  [excluded] {sender_name}")
                total_links = 0
                with open(out_path, 'w', encoding='utf-8') as out:
                    out.write(f"# {sender_name} — {_day_label(date_str)}\n\n")
                    out.write("> *This sender is on the exclude list — links are "
                              "listed but not summarized.*\n\n")
                    for em in sender_emails:
                        out.write(f"## {em['subject']}\n\n")
                        all_links = list(em['story_links'])
                        seen_urls = {l['url'] for l in all_links}
                        for pl in extract_paywall_article_urls(em['raw_html']):
                            if pl['url'] not in seen_urls:
                                all_links.append(pl)
                                seen_urls.add(pl['url'])
                        total_links += len(all_links)
                        for link in all_links:
                            out.write(f"- [{link['title']}]({link['url']})\n")
                        out.write("\n")
                        apply_label(service, em['message_id'], label_id)
                        newest_date_ms = max(newest_date_ms, em['internal_date_ms'])
                excluded_rows.append({
                    'sender': sender_name,
                    'emails': len(sender_emails),
                    'links':  total_links,
                })
                continue

            total_links_all   = 0
            summarized_count  = 0
            has_any_articles  = False
            email_plans       = sender_plans[sender_name]

            with open(out_path, 'w', encoding='utf-8') as out:
                out.write(f"# {sender_name} — {_day_label(date_str)}\n\n")

                for em, all_links, capped, payload in email_plans:
                    out.write(f"## {em['subject']}\n\n")

                    if capped:
                        has_any_articles = True
                        total_links_all += len(all_links)
                        if len(all_links) > MAX_ARTICLES_PER_EMAIL:
                            out.write(f"> *Showing {MAX_ARTICLES_PER_EMAIL} of "
                                      f"{len(all_links)} articles.*\n\n")

                        for link, future in zip(capped, payload):
                            source, hit_paywall, summary = future.result()
                            paywall_note = (
                                "\n> ⚠️ **Paywalled** — summary based on free preview "
                                "only. Full article not retrievable.\n"
                                if hit_paywall and source == 'fallback' else ''
                            )

                            out.write(f"### {link['title']}\n\n")
                            out.write(f"> 🔗 {link['url']}  |  📥 {source}\n")
                            out.write(f"{paywall_note}\n")
                            out.write(f"{summary}\n\n")
                            out.write("---\n\n")
                            summarized_count += 1

                    else:
                        summary = payload.result()
                        out.write(f"{summary}\n\n")
                        out.write("---\n\n")
                        body_only_rows.append({
                            'sender':  sender_name,
                            'subject': em['subject'],
                        })

                    apply_label(service, em['message_id'], label_id)
                    newest_date_ms = max(newest_date_ms, em['internal_date_ms'])

            if has_any_articles:
                sender_rows.append({
                    'sender':     sender_name,
                    'emails':     len(sender_emails),
                    'summarized': summarized_count,
                    'total':      total_links_all,
                })

        write_day_index(day_dir, date_str, sender_rows, body_only_rows,
                        excluded_rows, skipped_rows)
        print(f"\n  ✓ {date_str}/ written  ({len(sender_rows)} newsletter files + _index.md)")

        # Persist incrementally after each day, not just at the very end —
        # a crash partway through a multi-day batch still keeps whatever
        # cache entries were computed for the days that finished.
        save_article_cache(article_cache)

        for em in transactional:
            newest_date_ms = max(newest_date_ms, em['internal_date_ms'])

    article_pool.shutdown(wait=True)
    save_article_cache(article_cache)

    if newest_date_ms > (cursor_ms or 0):
        save_cursor(newest_date_ms)
        print(f"\nCursor updated → {datetime.fromtimestamp(newest_date_ms/1000).strftime('%Y-%m-%d %H:%M:%S')}")

    print("\nDone.")
