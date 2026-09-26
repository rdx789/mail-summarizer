"""
Compare two mlx_lm models on one email/article, read-only (no label, no
cursor change).

  python compare_models.py prepare [url]      # fetch a URL (default: first article of a recent email) -> sample.json
  python compare_models.py run <mlx-model>    # summarize sample.json with that model -> result-<slug>.json
  python compare_models.py report             # side-by-side report of all result-*.json

`run` is a separate process per model on purpose: local_llm.py loads the
model at import time, so each model needs a fresh interpreter (and the
first one's memory is fully released before the next loads).
"""
import json
import os
import sys
import time

from dotenv import load_dotenv

load_dotenv()

WORK_DIR = os.environ.get('COMPARE_DIR', '.')
SAMPLE = os.path.join(WORK_DIR, 'sample.json')


def _slug(model):
    return model.replace('/', '__')


def prepare_url(url):
    os.environ['LLM_BACKEND'] = 'ollama'
    from fetch_article import fetch_with_fallbacks
    content, source, paywalled = fetch_with_fallbacks(url, verbose=False)
    if source == 'fallback' or len(content) < 2000:
        print(f"Could not fetch a full article from {url}")
        sys.exit(1)
    with open(SAMPLE, 'w', encoding='utf-8') as f:
        json.dump({'subject': '', 'sender': '', 'title': url, 'url': url,
                   'source': source, 'content': content}, f)
    print(f"Sample: {url} ({len(content)} chars via {source})")


def prepare():
    # main.py imports local_llm, which would load the mlx model at import
    # time; this step needs no LLM, so point it at a backend that loads nothing.
    os.environ['LLM_BACKEND'] = 'ollama'
    from gmail_auth import get_gmail_service
    from fetch_article import fetch_with_fallbacks
    from main import parse_and_clean_email_body

    service = get_gmail_service()
    msgs = service.users().messages().list(userId='me', q='in:inbox', maxResults=40).execute().get('messages', [])
    for m in msgs:
        em = parse_and_clean_email_body(service, 'me', m['id'])
        for link in em['story_links']:
            try:
                content, source, paywalled = fetch_with_fallbacks(link['url'], em['body'], verbose=False)
            except Exception as exc:
                print(f"  fetch failed for {link['url']}: {exc}")
                continue
            if source == 'fallback' or len(content) < 2000:
                continue  # failed fetch, or a promo/redirect stub, not an article
            sample = {'subject': em['subject'], 'sender': em['sender_name'],
                      'title': link['title'], 'url': link['url'], 'source': source,
                      'content': content}
            with open(SAMPLE, 'w', encoding='utf-8') as f:
                json.dump(sample, f)
            print(f"Sample: {em['sender_name']} · {link['title']}\n  {link['url']} ({len(content)} chars via {source})")
            return
    print("No email with a fetchable article found in the last 40 inbox messages.")
    sys.exit(1)


def run(model):
    os.environ['LLM_BACKEND'] = 'mlx_lm'
    os.environ['MLX_MODEL'] = model
    from local_llm import generate, strip_thinking, strip_preamble
    from main import load_summary_prompt

    with open(SAMPLE, encoding='utf-8') as f:
        sample = json.load(f)
    prompt = load_summary_prompt()

    generate("Say ok.", system_prompt="Be terse.")  # warm-up, keeps load/compile time out of the timing
    t0 = time.perf_counter()
    raw = generate(sample['content'], system_prompt=prompt)
    elapsed = time.perf_counter() - t0
    summary = strip_preamble(strip_thinking(raw))

    out = {'model': model, 'seconds': round(elapsed, 1), 'chars': len(summary),
           'summary': summary}
    with open(os.path.join(WORK_DIR, f'result-{_slug(model)}.json'), 'w', encoding='utf-8') as f:
        json.dump(out, f)
    print(f"{model}: {elapsed:.1f}s, {len(summary)} chars")


def report():
    with open(SAMPLE, encoding='utf-8') as f:
        sample = json.load(f)
    results = []
    for name in sorted(os.listdir(WORK_DIR)):
        if name.startswith('result-') and name.endswith('.json'):
            with open(os.path.join(WORK_DIR, name), encoding='utf-8') as f:
                results.append(json.load(f))
    sections = ['TL;DR', 'Problem & Context', 'Solution & Architecture',
                'Code & Implementation', 'Results & Trade-offs', 'Key Takeaways']
    lines = [f"# {sample['title']}\n", f"{sample['url']} · {len(sample['content'])} chars input\n",
             "| Model | Time | Chars | Sections present |", "|---|---|---|---|"]
    for r in results:
        have = sum(s in r['summary'] for s in sections)
        lines.append(f"| {r['model']} | {r['seconds']}s | {r['chars']} | {have}/{len(sections)} |")
    for r in results:
        lines += ["", f"## {r['model']}", "", r['summary']]
    path = os.path.join(WORK_DIR, 'comparison.md')
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
    print(f"Wrote {path}")


if __name__ == '__main__':
    try:
        cmd = sys.argv[1]
        if cmd == 'prepare':
            prepare_url(sys.argv[2]) if len(sys.argv) == 3 else prepare()
        elif cmd == 'run' and len(sys.argv) == 3:
            run(sys.argv[2])
        elif cmd == 'report':
            report()
        else:
            raise IndexError
    except IndexError:
        print(__doc__)
        sys.exit(2)
