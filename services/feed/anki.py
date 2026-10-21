"""kumapie on the PC <-> kuma3 desktop through AnkiConnect Extended (code\\kumapie_pc_plan.md, "The Anki bridge").

The phone's rules, ported: a word's own cards are its language's Mined deck only, keyed by the Word field (CardIndex.kt);
one rating answers every due card of the word (KnownWords.rate); a mined card is the 🐻 note type's layout (Miner.kt).
Run this file for its self-check (no Anki needed).
"""
import base64
import html
import json
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

URL = "http://127.0.0.1:8765"
DECKS = {"de": "Deutsch Mined", "en": "English Mined", "ja": "Japanese Mined"}
NOTE_TYPES = {"de": ["🐻 German", "🇩🇪 MvJ"], "en": ["🐻 English", "🇺🇸 MvJ"], "ja": ["🐻 Japanese", "🇯🇵 MvJ+"]}
DEF_BI, DEF_MONO = "Definition (bilingual)", "Definition (monolingual)"
synced_at = 0  # last sync from here (epoch)


class Closed(Exception):
    """kuma3 desktop isn't running (or AnkiConnect isn't installed)."""


def ac(action, **params):
    req = json.dumps({"action": action, "version": 6, "params": params}).encode()
    try:
        with urllib.request.urlopen(URL, req, timeout=120) as r:
            out = json.load(r)
    except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
        raise Closed(str(e))
    if out.get("error"):
        raise ValueError(f"AnkiConnect {action}: {out['error']}")
    return out["result"]


def up() -> bool:
    try:
        return ac("version") >= 6
    except (Closed, ValueError):
        return False


# ------------------------------------------------------------ word keys (CardIndex.kt)

def plain(field: str) -> str:
    return html.unescape(re.sub(r"<[^>]*>", "", field))


def form(field: str) -> str:
    """German, English: the Word field before "[", no HTML, lowercase ("macht[→ machen]" -> "macht")."""
    return plain(field).split("[")[0].strip().lower()


def ja_spelling(field: str) -> str:
    """Japanese: the Word field's spelling (`日本[にほん] 語[ご]:0-` -> 日本語), the key subtitles colour by."""
    s = re.sub(r"[/*\\-]", "", re.sub(r":[0-9A-Za-z\-]*", "", plain(field)))
    return re.sub(r"\s", "", re.sub(r"([^\s\[\]]+)\[[^\]]*]", r"\1", s))


def key(lang: str, field: str) -> str:
    return ja_spelling(field) if lang == "ja" else form(field)


def state(own: list[dict], due: set[int]) -> str:
    """d due today (review or learning), b mined and never reviewed, k studied and not due (CardIndex.state)."""
    if any(c["cardId"] in due for c in own):
        return "d"
    return "b" if all(c["type"] == 0 for c in own) else "k"


def index(lang: str) -> tuple[dict[str, list[dict]], set[int]]:
    """Form -> its cards in the Mined deck, and today's due cards."""
    deck = f'"deck:{DECKS[lang]}"'
    cards = ac("cardsInfo", cards=ac("findCards", query=deck))
    by_form: dict[str, list[dict]] = {}
    for c in cards:
        k = key(lang, (c["fields"].get("Word") or {}).get("value", ""))
        if k:
            by_form.setdefault(k, []).append(c)
    return by_form, set(ac("findCards", query=f"{deck} is:due"))


def cards(lang: str) -> dict:
    """{"forms": {form: state}, "notes": {form: first note id}} for the page's colours and "Open in Anki"."""
    by_form, due = index(lang)
    return {"forms": {k: state(cs, due) for k, cs in by_form.items()},
            "notes": {k: cs[0]["note"] for k, cs in by_form.items()}}


def rate(lang: str, word: str, ease: int) -> dict:
    """Answers the word's due cards (none due: its first card, an early review) with [ease] 1-4."""
    if ease not in (1, 2, 3, 4):
        raise ValueError("ease 1-4")
    by_form, due = index(lang)
    own = by_form.get(key(lang, word), [])
    answer = [c for c in own if c["cardId"] in due] or own[:1]
    if not answer:
        raise ValueError("no card: mine it first")
    done = ac("answerCards", answers=[{"cardId": c["cardId"], "ease": ease} for c in answer])
    return {"answered": sum(1 for x in done if x), "cards": len(answer)}


def undo() -> dict:
    return {"ok": bool(ac("guiUndo"))}


def sync() -> dict:
    global synced_at
    ac("sync")
    synced_at = int(time.time())
    return {"ok": True, "synced": synced_at}


def browse(note: int) -> dict:
    ac("guiBrowse", query=f"nid:{int(note)}")
    return {"ok": True}


def delete(note: int) -> dict:
    ac("deleteNotes", notes=[int(note)])
    return {"ok": True}


# ------------------------------------------------------------ mining (Miner.kt)

def esc(s: str) -> str:
    return html.escape(s, quote=True)


def slug(s: str) -> str:
    """"You and I Are Polar Opposites" -> "you_and_i_are_polar_opposites", the show tag."""
    return re.sub(r"[\W_]+", "_", s.lower()).strip("_")


def cue_text(cue: dict) -> str:
    return "".join(t for t, _ in cue["seg"])


def sentence(cues: list[dict], line: int) -> tuple[str, list[dict]]:
    """The whole sentence cue [line] is part of: cues joined and cut at . ! ? … before a capital (Miner.sentence)."""
    def ends(i):
        text = cue_text(cues[i]).rstrip()
        if i + 1 >= len(cues):
            return True
        nxt = cue_text(cues[i + 1]).lstrip()
        return not text or (text[-1] in ".!?…\"»“" and (not nxt or not nxt[0].islower()))
    first = line
    while first > 0 and not ends(first - 1):
        first -= 1
    last = line
    while last < len(cues) - 1 and not ends(last):
        last += 1
    part = cues[first:last + 1]
    return " ".join(cue_text(c).strip() for c in part), part


def english(scene: dict, cues: list[dict]) -> str:
    start, end = cues[0]["start"], cues[-1]["end"]
    lines = [e for e in scene["english"] if len(e) > 2 and e[1] > start + 0.1 and e[0] < end - 0.1]
    return " ".join(e[2] for e in (lines or [e for e in scene["english"] if len(e) > 2]))


def definition(written: str, lemma: str, gloss: str) -> str:
    """The Core 1000 layout: "<b>word</b> (lemma)", then the meaning."""
    lem = f" ({esc(lemma)})" if lemma and lemma.lower() != written.lower() else ""
    g = f"<br><b>{esc(gloss)}</b>" if gloss.strip() else ""
    return f'<span style="font-size:1.4em"><b>{esc(written)}</b>{lem}</span>{g}'


def ffmpeg_cut(ffmpeg: str, args: list[str]) -> None:
    r = subprocess.run([ffmpeg, "-v", "error", "-y", *args], capture_output=True, text=True, timeout=300)
    if r.returncode:
        raise ValueError(f"ffmpeg: {r.stderr.strip()[-300:]}")


def store(path: Path, name: str) -> str:
    """Into kuma3's media folder (sent as data: the service account's temp folder may not be Anki's to read)."""
    return ac("storeMediaFile", filename=name, data=base64.b64encode(path.read_bytes()).decode())


def mine(ep: dict, video: Path, ffmpeg: str, word_audio, body: dict) -> dict:
    """A card from the page: [body] = {scene, line, word?, surface?, gloss?, english?}. [ep] is tv.episode()'s JSON;
    [word_audio](word, lang, reading=None) -> audio Path (mp3 or opus) or None. Answers {"note", "deck"}."""
    lang = ep["lang"]
    if lang not in DECKS:
        raise ValueError("no Mined deck for " + lang)
    sc = next((s for s in ep["scenes"] if s["id"] == body.get("scene")), None)
    line = int(body.get("line", 0))
    if not sc or not 0 <= line < len(sc["cues"]):
        raise ValueError("unknown scene or line")
    models = ac("modelNames")
    model = next((m for m in NOTE_TYPES[lang] if m in models), None)
    if not model:
        raise ValueError(f"no {NOTE_TYPES[lang][0]} note type in kuma3")
    names = ac("modelFieldNames", modelName=model)
    word = body.get("word") or None
    surface = body.get("surface") or word or ""
    info = ep["words"].get(word, {}) if word else {}
    show = slug(ep["show"])
    japanese = lang == "ja"
    text, cues = (cue_text(sc["cues"][line]).strip(), [sc["cues"][line]]) if japanese else sentence(sc["cues"], line)
    fields: dict[str, str] = {}
    tags = [show, "kumapie", f"_{lang}::{'word' if word else 'sentence'}"]

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        start, end = max(cues[0]["start"] - 0.25, 0), cues[-1]["end"] + 0.25
        ms = f"{int(start * 1000)}-{int(end * 1000)}"
        # As the phone's (Media3 AAC, ~600 kbps): a 94 kbps MP3 sounded unlike the other cards (Pedro, 2026-10-15).
        ffmpeg_cut(ffmpeg, ["-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", str(video), "-vn", "-c:a", "aac",
                            "-b:a", "320k", str(tmp / "line.m4a")])
        line_file = store(tmp / "line.m4a", f"{show}_{ms}.m4a")
        if "Video" in names:  # the front plays word audio -> sentence audio -> the scene's clip
            s0, s1 = sc["start"], sc["end"]
            # The phone's WebM: VP9 480p ~280 kbps, keyframe every 10 s, Opus 64 kbps (the template plays .webm only).
            # Realtime VP9 ignores a plain -b:v (670 kbps out); -crf 45 with it lands near the phone's 360-540 kbps.
            ffmpeg_cut(ffmpeg, ["-ss", f"{s0:.3f}", "-to", f"{s1:.3f}", "-i", str(video), "-vf", "scale=-2:480",
                                "-c:v", "libvpx-vp9", "-crf", "45", "-b:v", "280k", "-g", "240", "-deadline", "realtime", "-cpu-used", "8",
                                "-c:a", "libopus", "-b:a", "64k", str(tmp / "clip.webm")])
            clip = store(tmp / "clip.webm", f"{show}_{int(s0 * 1000)}-{int(s1 * 1000)}.webm")
            fields["Video"] = f"[audio:{line_file}][audio:{clip}]"
        else:  # 🐻 Japanese, 🐻 English: the line's audio and a picture
            fields["Sentence Audio"] = f"[audio:{line_file}]"
            if "Image" in names:
                ffmpeg_cut(ffmpeg, ["-ss", f"{(start + end) / 2:.3f}", "-i", str(video), "-frames:v", "1",
                                    "-vf", "scale=-2:480", "-q:v", "80", str(tmp / "shot.webp")])
                fields["Image"] = f'<img src="{store(tmp / "shot.webp", f"{show}_{int(start * 1000)}.webp")}">'

        reading = None
        if word:
            gloss = str(body.get("gloss") or info.get("g") or "")
            lemma = info.get("lemma") or ""
            if japanese:
                expression = lemma or surface
                reading = info.get("r") if any("㐀" <= ch <= "鿿" for ch in expression) else None
                fields["Word"] = esc(expression) + (f"[{esc(reading)}]" if reading else "")
                fields[DEF_BI] = esc(gloss)
                if surface != expression:
                    reading = None  # the sentence shows the reading only when written as in the dictionary
                spoken = reading or expression
            else:
                written = word if " " in word else surface
                fields["Word"] = esc(written) + (f"[→ {esc(lemma)}]" if lemma and lemma.lower() != written.lower() else "")
                fields[DEF_BI] = definition(written, lemma, gloss)
                spoken = surface
            audio = word_audio(expression, lang, info.get("r")) if japanese else word_audio(word, lang)
            if audio:
                fields["Word Audio"] = f"[audio:{store(audio, 'kumapie-' + slug(spoken) + audio.suffix)}]"

    at = text.find(surface) if japanese and surface else -1
    fields["Sentence"] = esc(text) if at < 0 else (esc(text[:at]) + "<b>" + esc(surface) + (f"[{esc(reading)}]" if reading else "")
                                                     + "</b>" + esc(text[at + len(surface):]))
    fields["Notes"] = esc(str(body.get("english") or english(sc, cues)))
    fields["Context"] = esc(f"Mined in kumapie: {ep['show']} · {ep['title']}, scene {sc['i'] + 1}.")
    missing = [f for f in ["Sentence"] + ([DEF_BI] if word else []) if f not in names]
    if missing:
        raise ValueError(f"{model} has no field {', '.join(missing)}")
    note = ac("addNote", note={"deckName": DECKS[lang], "modelName": model, "tags": tags,
                               "fields": {n: fields.get(n, "") for n in names},
                               "options": {"allowDuplicate": True}})
    return {"note": note, "deck": DECKS[lang]}


if __name__ == "__main__":
    assert form("macht[→ machen]") == "macht"
    assert form("Wenn&#39;s") == "wenn's"
    assert form("<b>Haben</b>[hat, hatte]") == "haben"
    assert ja_spelling("日本[にほん] 語[ご]:0-") == "日本語"
    assert ja_spelling("拾[ひろ]\\う:0") == "拾う"
    assert ja_spelling("みんな:3-") == "みんな"
    c = lambda i, t: {"cardId": i, "type": t}
    assert state([c(1, 2)], {1}) == "d"
    assert state([c(1, 2)], set()) == "k"
    assert state([c(1, 0)], set()) == "b"
    assert state([c(1, 1)], {1}) == "d"  # learning, due
    assert state([c(1, 1)], set()) == "k"  # learning, not due: white
    cue = lambda s: {"seg": [[s, None]], "start": 0, "end": 1}
    cues = [cue("Aber warum…"), cue("fühle ich mich"), cue("ausgelaugt?"), cue("Keine Ahnung.")]
    assert sentence(cues, 1) == ("Aber warum… fühle ich mich ausgelaugt?", cues[:3])
    assert sentence(cues, 3)[0] == "Keine Ahnung."
    assert slug("You and I Are Polar Opposites") == "you_and_i_are_polar_opposites"
    print("anki.py self-check ok")
