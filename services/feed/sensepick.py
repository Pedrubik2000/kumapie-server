"""Which Daijirin sense a Japanese word has in its line: three cloud models vote (2 must agree, none may say unsure).

Test results (C:\\dojo\\work\\sensepick, 2026-11-14): Flash-Lite + ERNIE 5.5 Flash + Qwen 3.8 with examples and set
phrases in the prompt put a ✓ on 92/97 lookups, 1 wrong, none on 10 garbled lines. The local Qwen3 8B got 74% alone.

Keys in .env: GEMINI_API_KEY (Flash-Lite, free 500/day), GROQ_API_KEY (Qwen, free ~200 lookups/day),
OPENROUTER_API_KEY (ERNIE free; also the paid fallback for Flash-Lite and Qwen when a free tier says 429).
Daijirin: [kumapie.sensepick] daijirin in config.toml (the es-jp zip: Spanish + Japanese per sense).

    python sensepick.py 突っ込む つっこむ "小蘭 そっちの器 中に突っ込んで"
"""
import json, os, pickle, re, sys, threading, time, urllib.error, urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from dojo.config import cfg  # noqa: E402

CONF = cfg.raw.get("kumapie", {}).get("sensepick", {})
ZIP = cfg.root / CONF.get("daijirin", "state/dicts/yomitan-ja/daijirin4-opus-es-jp.zip")
CACHE = cfg.root / "state/feed/sensepick-daijirin.pkl"

# (name, url, model, key env) - free first; OpenRouter paid only when a free tier refuses.
MODELS = [
    [("gemini", "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions", "gemini-3.1-flash-lite", "GEMINI_API_KEY"),
     ("openrouter", "https://openrouter.ai/api/v1/chat/completions", "google/gemini-3.1-flash-lite", "OPENROUTER_API_KEY")],
    [("openrouter", "https://openrouter.ai/api/v1/chat/completions", "baidu/ernie-5.5-flash-preview:free", "OPENROUTER_API_KEY")],
    [("groq", "https://api.groq.com/openai/v1/chat/completions", "qwen/qwen3.8-27b", "GROQ_API_KEY"),
     ("openrouter", "https://openrouter.ai/api/v1/chat/completions", "qwen/qwen3.8-27b", "OPENROUTER_API_KEY")],
]

_dic = None
_lock = threading.Lock()
_results: dict = {}  # ponytail: in-memory cache, gone on restart; a file if re-asks get costly


def _text(x):
    if isinstance(x, str): return x
    if isinstance(x, dict): return _text(x.get("content"))
    if isinstance(x, list): return "".join(_text(y) for y in x)
    return ""


def _example(node, stem):
    out = []
    def walk(x):
        if isinstance(x, str): out.append(x.replace("━", stem))  # some examples have a bare ━
        elif isinstance(x, dict):
            n = x.get("data", {}).get("name")
            if n == "語幹相当部": out.append(stem)
            elif n != "活用分節": walk(x.get("content"))
        elif isinstance(x, list):
            for y in x: walk(y)
    walk(node.get("content"))
    return "".join(out).strip()


def _nodes(x):
    """語釈 (sense) and 用例 (example) nodes in document order."""
    if isinstance(x, dict):
        if x.get("data", {}).get("name") in ("語釈", "用例"):
            yield x
            return
        yield from _nodes(x.get("content"))
    elif isinstance(x, list):
        for y in x: yield from _nodes(y)


_LATIN = re.compile("[A-Za-z]")


def _split(content):
    """(spanish, japanese) of a 語釈: the first bare " " after which no text has Latin letters."""
    if isinstance(content, list):
        for j, el in enumerate(content):
            if el == " " and not _LATIN.search(_text(content[j + 1:])):
                return _text(content[:j]).strip(), _text(content[j + 1:]).strip()
    t = _text(content).strip()
    return ("", t) if not _LATIN.search(t) else (t, "")


def _build():
    import zipfile
    d = {}
    with zipfile.ZipFile(ZIP) as z:
        for name in z.namelist():
            if not name.startswith("term_bank_"): continue
            for row in json.loads(z.read(name)):
                stem = re.sub("[ぁ-ん]+$", "", row[0]) or row[0]
                senses = []
                for n in _nodes(row[5]):
                    if n["data"]["name"] == "語釈":
                        es, ja = _split(n["content"])
                        senses.append({"es": es, "ja": ja, "ex": []} if ja else None)
                    elif senses and senses[-1] is not None:
                        senses[-1]["ex"].append(_example(n, stem))
                senses = [s for s in senses if s]
                if senses: d.setdefault(row[0], []).append((row[1], senses))
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    pickle.dump(d, open(CACHE, "wb"))
    return d


def daijirin():
    global _dic
    with _lock:
        if _dic is None:
            _dic = pickle.load(open(CACHE, "rb")) if CACHE.exists() and CACHE.stat().st_mtime > ZIP.stat().st_mtime else _build()
        return _dic


def senses(term, reading):
    """All senses of the entries for term whose reading matches (all readings when none matches)."""
    entries = daijirin().get(term, [])
    hit = [s for r, ss in entries if r == reading for s in ss]
    return hit or [s for r, ss in entries for s in ss]


def needs_pick(ss) -> bool:
    """Many senses or a long one: where a ✓ saves reading. ponytail: fixed thresholds, tune from use."""
    return len(ss) >= 4 or (len(ss) >= 2 and any(len(s["es"]) + len(s["ja"]) > 80 for s in ss))


def _idioms(term, sentence):
    """Daijirin set phrases with this word that appear in the sentence (目が合う in 目が合った)."""
    stem = re.sub("[ぁ-ん]+$", "", term) or term
    out = []
    for h, entries in daijirin().items():
        if len(h) < 3 or h == term or stem not in h: continue
        if not (set(re.findall("[一-龯]", h)) - set(stem) or re.search("[をがにでと]", h[len(stem):])): continue
        hs = re.sub("[ぁ-ん]$", "", h)
        if len(hs) > len(stem) and hs in sentence:
            out += [f"{h}（{r}）: " + " / ".join(s["ja"][:60] for s in ss[:3]) for r, ss in entries]
    return out[:5]


def _prompt(term, reading, sentence, ss):
    lines = "\n".join(f"{i}. {s['ja'][:80]}" + (" 例:" + "".join(e[:30] for e in s["ex"][:2]) if s["ex"] else "")
                      for i, s in enumerate(ss[:30], 1))
    ids = _idioms(term, sentence)
    hint = ("Set phrases with this word found in the sentence (Daijirin; a hint, still answer with one of the numbered "
            "senses):\n" + "\n".join(ids) + "\n") if ids else ""
    return ("Pick the dictionary sense (Daijirin) that the word has in this Japanese sentence. If the sentence is "
            "garbled, cut off, does not contain the word, or you cannot tell, answer unsure.\n\n"
            f"{hint}Sentence: {sentence}\nWord: {term} (reading: {reading})\nSenses:\n{lines}\n\n"
            'First give a one-line reason in the form "Reason: ...", then "Answer: <number or unsure>".')


def _ask(route, prompt):
    """One model's answer: a sense number as a string, 'unsure', or None (no answer / every route failed)."""
    for name, url, model, key in route:
        k = os.environ.get(key)
        if not k: continue
        body = {"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0, "max_tokens": 2000}
        req = urllib.request.Request(url, json.dumps(body).encode(), {
            "Content-Type": "application/json", "Authorization": "Bearer " + k,
            "User-Agent": "dojo-feed"})  # Groq 403s urllib's default User-Agent
        try:
            r = json.load(urllib.request.urlopen(req, timeout=8))
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            print(f"sensepick {name} {model}: {e}", flush=True)
            continue
        m = re.findall(r"Answer:\s*(unsure|\d+)", r["choices"][0]["message"].get("content") or "")
        return m[-1] if m else None
    return None


def check(term: str, reading: str) -> bool:
    """Fast: does this word get the AI box (so the app can show "loading…" before the vote)?"""
    ss = senses(term, reading)
    return bool(ss) and needs_pick(ss)


def pick(term: str, reading: str, sentence: str) -> dict:
    """{"pick": false} when the entry doesn't need one; else {"pick": true, "sense": {es, ja, ex} | null, "votes": [...]}.
    Returns as soon as two models agree; an "unsure" before that means no ✓."""
    ss = senses(term, reading)
    if not ss or not needs_pick(ss):
        return {"pick": False}
    key = (term, reading, sentence)
    if key in _results: return _results[key]
    prompt = _prompt(term, reading, sentence, ss)
    ex = ThreadPoolExecutor(len(MODELS))
    futures = [ex.submit(_ask, r, prompt) for r in MODELS]
    votes, n = [], None
    for f in as_completed(futures):
        votes.append(f.result())
        if "unsure" in votes: break
        top = Counter(v for v in votes if v and v.isdigit()).most_common(1)
        if top and top[0][1] >= 2:
            n = int(top[0][0])
            break
    ex.shutdown(wait=False)  # the slow third model finishes in the background, unused
    out = {"pick": True, "sense": ss[n - 1] if n and n <= len(ss) else None, "votes": votes}
    _results[key] = out
    return out


if __name__ == "__main__":
    t = time.time()
    print(json.dumps(pick(*sys.argv[1:4]), ensure_ascii=False, indent=1), f"{time.time() - t:.1f}s")
