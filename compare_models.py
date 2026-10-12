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
import re
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

    # Capture mlx_lm's own speed stats without touching local_llm.py: swap its
    # mlx_lm.generate for a stream_generate wrapper that returns the same text
    # (same rendered prompt, max_tokens and sampler) and records the last stats.
    import mlx.core as mx
    import local_llm
    stats = {}
    real_mlx_lm = local_llm.mlx_lm

    class _Timed:
        def __getattr__(self, name):
            return getattr(real_mlx_lm, name)

        @staticmethod
        def generate(m, tok, prompt, **kw):
            text, last = '', None
            for last in real_mlx_lm.stream_generate(m, tok, prompt=prompt, **kw):
                text += last.text
            stats.update(prompt_tokens=last.prompt_tokens, prompt_tps=last.prompt_tps,
                         gen_tokens=last.generation_tokens, gen_tps=last.generation_tps,
                         peak_gb=mx.get_peak_memory() / 1e9)
            return text

    generate("Say ok.", system_prompt="Be terse.")  # warm-up, keeps load/compile time out of the timing
    local_llm.mlx_lm = _Timed()
    t0 = time.perf_counter()
    raw = generate(sample['content'], system_prompt=prompt)
    elapsed = time.perf_counter() - t0
    local_llm.mlx_lm = real_mlx_lm
    summary = strip_preamble(strip_thinking(raw))

    out = {'model': model, 'seconds': round(elapsed, 1), 'chars': len(summary),
           'summary': summary, 'raw_chars': len(raw), **{k: round(v, 2) for k, v in stats.items()}}
    with open(os.path.join(WORK_DIR, f'result-{_slug(model)}.json'), 'w', encoding='utf-8') as f:
        json.dump(out, f)
    print(f"{model}: {elapsed:.1f}s, {stats.get('gen_tokens')} tok @ {stats.get('gen_tps', 0):.1f} tok/s decode, "
          f"{stats.get('prompt_tps', 0):.0f} tok/s prefill, {len(summary)} chars")


def _numbers(text):
    """Numbers worth checking: 2+ digits, or with a decimal point/percent sign."""
    found = set()
    for m in re.findall(r'\d[\d,]*\.?\d*%?', text):
        m = m.rstrip('.,').replace(',', '')
        if len(re.sub(r'\D', '', m)) >= 2 or '.' in m or '%' in m:
            found.add(m)
    return found


_UNITS = {w: i for i, w in enumerate(
    'zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen '
    'sixteen seventeen eighteen nineteen'.split())}
_TENS = {w: 10 * i for i, w in enumerate('_ _ twenty thirty forty fifty sixty seventy eighty ninety'.split())}
_SCALES = {'hundred': 100, 'thousand': 1000, 'million': 10**6, 'billion': 10**9}


def _word_numbers(text):
    """Numbers the source spells out ("three-thousand", "twenty five"), as digit strings."""
    found, total, current, in_num = set(), 0, 0, False

    def flush():
        nonlocal total, current, in_num
        if in_num and total + current >= 10:  # single digits are ignored, like _numbers() does
            found.add(str(total + current))
        total, current, in_num = 0, 0, False

    for w in re.findall(r'[a-z]+|[^a-z\s-]', text.lower()):  # hyphens/spaces join number words, other punctuation ends the number
        if w in _UNITS or w in _TENS:
            current += _UNITS.get(w, 0) + _TENS.get(w, 0)
            in_num = True
        elif w in _SCALES and in_num:
            if _SCALES[w] == 100:
                current *= 100
            else:
                total, current = total + current * _SCALES[w], 0
        else:
            flush()
    flush()
    return found


def _code_key(text):
    """Whitespace-insensitive form of code: formatters differ on spaces inside
    brackets and after commas/colons, which doesn't change the code."""
    text = re.sub(r'\s+', ' ', text)
    return re.sub(r' ?([()\[\]{},:]) ?', r'\1', text).strip()


_COMMENT_ONLY = re.compile(r'^(#(?!!|include\b|define\b|if\b|ifdef\b|ifndef\b|endif\b|pragma\b)|//|/\*|\*/)')


def _is_comment_only(line):
    """Models often add `# Install X` above a real command. That is annotation,
    not altered code, so it isn't held against the summary. Shebangs and C
    preprocessor lines start with # but are code."""
    return bool(_COMMENT_ONLY.match(line))


def _accuracy(summary, source):
    """Cheap fidelity checks against the source text. Not a quality score —
    they catch invented numbers and altered code, nothing subtler."""
    src = source.replace(',', '')
    spelled = _word_numbers(source)
    nums = _numbers(summary)
    bad_nums = sorted(n for n in nums if n.rstrip('%') not in src and n.rstrip('%') not in spelled)
    code_lines = [ln.strip() for blk in re.findall(r'```.*?\n(.*?)```', summary, re.S)
                  for ln in blk.splitlines() if len(ln.strip()) >= 8 and not _is_comment_only(ln.strip())]
    src_key = _code_key(source)
    bad_code = [ln for ln in code_lines if _code_key(ln) not in src_key]
    return nums, bad_nums, code_lines, bad_code


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
             "| Model | Time | Out tokens | Decode tok/s | Prefill tok/s | Peak GB | Chars | Sections | "
             "Numbers not in source | Code lines not in source |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        have = sum(s in r['summary'] for s in sections)
        nums, bad_nums, code, bad_code = _accuracy(r['summary'], sample['content'])
        lines.append(f"| {r['model']} | {r['seconds']}s | {r.get('gen_tokens', '?')} | {r.get('gen_tps', '?')} | "
                     f"{r.get('prompt_tps', '?')} | {r.get('peak_gb', '?')} | {r['chars']} | {have}/{len(sections)} | "
                     f"{len(bad_nums)}/{len(nums)} {bad_nums[:6]} | {len(bad_code)}/{len(code)} |")
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
