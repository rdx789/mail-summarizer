"""
Replica of the `local-llm-backend` skill's scripts/local_llm.py (~/.claude/skills/local-llm-backend/). Keep the two copies byte-identical — edit one, then copy it over the other.

Drop-in swappable local-LLM backend. Copy into your project. Requires
.env to have already been loaded (e.g. via `from dotenv import load_dotenv;
load_dotenv()`) before this module is imported, and LLM_BACKEND +
its backend-specific variables set — see SKILL.md for the .env template.
"""
import os
import platform
import re
import threading

import requests


def _require_env(name):
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name} is not set. Add it to your .env file.")
    return value


LLM_BACKEND = _require_env('LLM_BACKEND')
if LLM_BACKEND not in ('ollama', 'openai_compatible', 'mlx_lm'):
    raise RuntimeError(
        f"LLM_BACKEND={LLM_BACKEND!r} is not valid — set it to 'ollama', "
        f"'openai_compatible', or 'mlx_lm' in .env."
    )

if LLM_BACKEND == 'ollama':
    OLLAMA_URL   = _require_env('OLLAMA_URL')
    OLLAMA_MODEL = _require_env('OLLAMA_MODEL')
elif LLM_BACKEND == 'openai_compatible':
    LLM_BASE_URL = _require_env('LLM_BASE_URL')
    LLM_MODEL    = _require_env('LLM_MODEL')
    LLM_API_KEY  = _require_env('LLM_API_KEY')
else:  # mlx_lm
    if platform.machine() != 'arm64':
        raise RuntimeError(
            "LLM_BACKEND=mlx_lm requires Apple Silicon (arm64) — "
            f"detected {platform.machine()!r}."
        )
    MLX_MODEL = _require_env('MLX_MODEL')
    try:
        import mlx_lm
        from mlx_lm.sample_utils import make_sampler as _mlx_make_sampler
    except ImportError as exc:
        raise RuntimeError(
            "LLM_BACKEND=mlx_lm requires the mlx-lm package — "
            "install it with `pip install mlx-lm`."
        ) from exc
    _mlx_model, _mlx_tokenizer = mlx_lm.load(MLX_MODEL)
    # mlx_lm.generate isn't thread-safe on a shared in-process model, and
    # main.py calls generate() from several worker threads — serialize it.
    _mlx_lock = threading.Lock()


def strip_thinking(text):
    """Strip <think>...</think> reasoning blocks some local models (Qwen3
    family, DeepSeek-R1, etc.) emit inline in the response."""
    return re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()


def strip_preamble(text):
    """With thinking disabled, some models (e.g. Qwen3.6) prepend a
    throwaway lead-in line before the real answer, e.g. 'Based on the text
    provided, here is the summary:'. Drop anything before the first markdown
    heading/bold marker, if one appears near the start — safe no-op if the
    response doesn't use markdown formatting to begin with."""
    match = re.search(r'(\*\*|#)', text[:300])
    if match and match.start() > 0:
        return text[match.start():].strip()
    return text


def _generate_ollama(prompt, system_prompt=None):
    full_prompt = f"{system_prompt}\n\n{prompt}" if system_prompt else prompt
    response = requests.post(
        OLLAMA_URL,
        json={
            "model":      OLLAMA_MODEL,
            "prompt":     full_prompt,
            "stream":     False,
            "keep_alive": "30m",  # stay loaded across a batch; caller unloads when done
            # Mirrors the openai_compatible path's enable_thinking=False:
            # skip the hidden reasoning pass for models that support it
            # (e.g. Qwen3.6). Harmless no-op on models/servers that don't.
            "think":      False,
            # Mirrors the openai_compatible path's request options so both
            # backends generate under matching conditions (same sampling
            # behavior, same max output, same context window) for a fair
            # comparison and consistent output length between backends.
            "options": {
                "temperature": 0.3,
                "num_predict":  3000,   # matches max_tokens
                "num_ctx":      16384,  # matches llama-server's --ctx-size
            },
        },
        timeout=180,
    )
    response.raise_for_status()
    return response.json()['response'].strip()


def _generate_openai_compatible(prompt, system_prompt=None):
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    response = requests.post(
        f"{LLM_BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {LLM_API_KEY}"},
        json={
            "model":       LLM_MODEL,
            "messages":    messages,
            # 3000, not 4000: real summaries from this prompt template median
            # ~450 tokens and max out around ~2600 tokens (measured across
            # 176 real summaries) — 3000 keeps comfortable headroom above
            # every observed case (including code-heavy ones) while cutting
            # the worst-case decode tail that dominates wall-clock time.
            "max_tokens":  3000,
            "temperature": 0.3,
            "stream":      False,
            # Best-effort: some models (e.g. Qwen3.6) expose an
            # enable_thinking chat-template variable that skips their hidden
            # reasoning pass entirely — several times faster, no meaningful
            # quality loss for most tasks. Harmless no-op on servers/models
            # that don't recognize this field. Check by GET-ing /props on
            # the server and grepping its chat_template for "enable_thinking"
            # if you want to confirm support before relying on the speedup.
            "chat_template_kwargs": {"enable_thinking": False},
        },
        timeout=180,
    )
    response.raise_for_status()
    return response.json()['choices'][0]['message']['content']


def _generate_mlx_lm(prompt, system_prompt=None):
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    try:
        # Best-effort, mirrors the openai_compatible/ollama enable_thinking
        # toggle: harmless no-op if this tokenizer's chat template doesn't
        # recognize the kwarg.
        rendered = _mlx_tokenizer.apply_chat_template(
            messages, add_generation_prompt=True, enable_thinking=False,
        )
    except TypeError:
        rendered = _mlx_tokenizer.apply_chat_template(
            messages, add_generation_prompt=True,
        )

    with _mlx_lock:
        return mlx_lm.generate(
            _mlx_model,
            _mlx_tokenizer,
            prompt=rendered,
            max_tokens=3000,  # matches the other two backends
            sampler=_mlx_make_sampler(temp=0.3),
        )


def generate(prompt, system_prompt=None):
    """Call the configured local LLM backend. Returns the raw completion
    text — apply strip_thinking() and strip_preamble() yourself if you want
    a clean answer with no reasoning blocks or throwaway lead-in lines."""
    if LLM_BACKEND == 'ollama':
        return _generate_ollama(prompt, system_prompt)
    if LLM_BACKEND == 'mlx_lm':
        return _generate_mlx_lm(prompt, system_prompt)
    return _generate_openai_compatible(prompt, system_prompt)


if __name__ == '__main__':
    print(f"LLM_BACKEND = {LLM_BACKEND}")
    result = generate("Say 'ok' and nothing else.", system_prompt="You are terse.")
    print(f"Response: {strip_preamble(strip_thinking(result))!r}")
