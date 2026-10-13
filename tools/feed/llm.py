"""Claude client for `feed.py gloss`: OpenRouter if OPENROUTER_API_KEY is set, else the Anthropic API
(ANTHROPIC_API_KEY). Keys come from the environment or .env (loaded by dojo.config)."""
import os
import threading
import time

import anthropic

OPENROUTER_URL = "https://openrouter.ai/api"
RPM = 18  # OpenRouter new-account limit is 20 requests/minute per model
_lock = threading.Lock()
_next_slot = [0.0]


def make_client():
    if os.environ.get("OPENROUTER_API_KEY"):
        return anthropic.Anthropic(
            base_url=OPENROUTER_URL, auth_token=os.environ["OPENROUTER_API_KEY"], api_key=None, max_retries=0
        )
    return anthropic.Anthropic(max_retries=0)


def model_id(name):
    """Bare Anthropic ids (claude-haiku-4-5) -> OpenRouter's (anthropic/claude-haiku-4.5) when on OpenRouter."""
    if os.environ.get("OPENROUTER_API_KEY") and "/" not in name:
        head, _, ver = name.rpartition("-")
        if head and head[-1].isdigit():
            head, _, major = head.rpartition("-")
            ver = f"{major}.{ver}"
        return f"anthropic/{head}-{ver}"
    return name


def text_of(response):
    return "".join(b.text for b in response.content if b.type == "text").strip()


def call(client, **kwargs):
    """messages.create with a global request-rate throttle and patient 429 retries."""
    for attempt in range(8):
        with _lock:
            wait = _next_slot[0] - time.monotonic()
            _next_slot[0] = max(_next_slot[0], time.monotonic()) + 60 / RPM
        if wait > 0:
            time.sleep(wait)
        try:
            return client.messages.create(**kwargs)
        except anthropic.RateLimitError:
            time.sleep(15 * (attempt + 1))
    raise RuntimeError("still rate limited after 8 attempts")


if __name__ == "__main__":
    os.environ["OPENROUTER_API_KEY"] = "x"
    assert model_id("claude-haiku-4-5") == "anthropic/claude-haiku-4.5"
    assert model_id("anthropic/claude-haiku-4.5") == "anthropic/claude-haiku-4.5"
    print("ok")
