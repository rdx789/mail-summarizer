"""
Fetches the most recent inbox email, lists its story links, then
fetch+summarizes the first valid one using the configured LLM_BACKEND
(via local_llm.py). Read-only — does not label the message or touch
last_run.json's cursor.
"""
import sys
import time

from dotenv import load_dotenv

load_dotenv()

from gmail_auth import get_gmail_service
from fetch_article import fetch_with_fallbacks
from local_llm import generate, strip_thinking, strip_preamble, LLM_BACKEND
from main import parse_and_clean_email_body, load_summary_prompt


if __name__ == '__main__':
    t_start = time.perf_counter()
    print(f"LLM_BACKEND = {LLM_BACKEND}\n")

    service = get_gmail_service()
    results = service.users().messages().list(
        userId='me', q='in:inbox', maxResults=1
    ).execute()

    messages = results.get('messages', [])
    if not messages:
        print("Inbox is empty.")
        sys.exit(0)

    email = parse_and_clean_email_body(service, 'me', messages[0]['id'])

    print(f"Email  : {email['subject']}")
    print(f"From   : {email['sender']}")
    print(f"Date   : {email['date']}")
    print(f"Links  : {len(email['story_links'])}")
    print()

    prompt = load_summary_prompt()

    if not email['story_links']:
        print("No story links — summarizing the email body instead.\n")
        t0 = time.perf_counter()
        raw = generate(email['body'], system_prompt=prompt)
        summary = strip_preamble(strip_thinking(raw))
        t1 = time.perf_counter()
        print("=" * 80)
        print(summary)
        print("=" * 80)
        print(f"\nSummarize: {t1 - t0:.1f}s  |  Total: {t1 - t_start:.1f}s")
        sys.exit(0)

    for i, link in enumerate(email['story_links'], 1):
        print(f"  {i}. {link['title']}")
        print(f"     {link['url']}")
    print()

    chosen = None
    content = source = hit_paywall = None
    fetch_time = 0

    for link in email['story_links']:
        print(f"Fetching: {link['url']}")
        try:
            t0 = time.perf_counter()
            content, source, hit_paywall = fetch_with_fallbacks(link['url'], email['body'])
            fetch_time = time.perf_counter() - t0
            chosen = link
            print(f"  → {len(content)} chars via {source}  ({fetch_time:.1f}s)"
                  f"{'  [paywalled]' if hit_paywall else ''}")
            break
        except Exception as e:
            print(f"  → failed: {e}")

    if not chosen:
        print("All links failed to fetch.")
        sys.exit(1)

    print("\n--- FETCHED CONTENT " + "-" * 60)
    print(content)
    print("-" * 80)

    print(f"\nSummarizing '{chosen['title']}' with {LLM_BACKEND} ...")
    t2 = time.perf_counter()
    raw = generate(content, system_prompt=prompt)
    summary = strip_preamble(strip_thinking(raw))
    t3 = time.perf_counter()

    print("\n" + "=" * 80)
    print(summary)
    print("=" * 80)
    print(f"\nFetch: {fetch_time:.1f}s  |  Summarize: {t3 - t2:.1f}s  |  Total: {t3 - t_start:.1f}s")
