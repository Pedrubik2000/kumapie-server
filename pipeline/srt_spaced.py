#!/usr/bin/env python3
"""
Build an SRT from a canonical transcript JSON for space-separated languages
(German, English, ...), where srt_watch.py's MeCab segmentation doesn't apply.

Cues break at sentence ends, pauses and speaker changes; long sentences are
split at commas or pauses near the middle. Lines wrap at LINE_CHAR_LIMIT.

Usage:
    python3 scripts/srt_spaced.py -o my_video my_video.json
"""

import argparse
import json
import os

LINE_CHAR_LIMIT = 42     # max characters per subtitle line
MAX_LINES = 2            # max lines per cue
MAX_CUE_SECONDS = 7.0    # split cues longer than this
PAUSE_BREAK = 0.8        # a pause this long always ends a cue
LEAD_IN = 0.5            # show a cue this long before the first word (STT word starts run late)
END_PAD = 0.4            # keep a cue on screen this long after the last word
MIN_CUE_SECONDS = 1.0    # stretch short cues up to this, if the gap allows

SENTENCE_END = (".", "?", "!", "…")
CLAUSE_END = (",", ";", ":", "–", "—")


def load_words(path):
    """Join the provider's tokens into whole words with start/end/speaker.

    Tokens may be sub-word pieces; a spacing token marks a word boundary.
    Punctuation tokens arrive without a preceding space and stick to the word.
    """
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    words, cur, new_word = [], None, True
    for t in data["words"]:
        if t["type"] == "spacing":
            new_word = True
            continue
        if t["type"] != "word":
            new_word = True  # audio events ([music] etc.) are dropped
            continue
        if new_word or cur is None or t["speaker_id"] != cur["speaker"]:
            cur = {"text": t["text"], "start": t["start"], "end": t["end"],
                   "speaker": t["speaker_id"]}
            words.append(cur)
        else:
            cur["text"] += t["text"]
            cur["end"] = t["end"]
        new_word = False
    return [w for w in words if w["text"].strip()]


def split_sentences(words):
    """Group words at sentence-final punctuation, long pauses, speaker changes."""
    groups, cur = [], []
    for i, w in enumerate(words):
        if cur and (w["speaker"] != cur[-1]["speaker"]
                    or w["start"] - cur[-1]["end"] >= PAUSE_BREAK):
            groups.append(cur)
            cur = []
        cur.append(w)
        if w["text"].endswith(SENTENCE_END):
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)
    return groups


def text_of(ws):
    return " ".join(w["text"] for w in ws)


def fits(ws):
    return (len(text_of(ws)) <= LINE_CHAR_LIMIT * MAX_LINES
            and ws[-1]["end"] - ws[0]["start"] <= MAX_CUE_SECONDS
            and wrap(text_of(ws)) is not None)


def split_long(ws):
    """Recursively split a group into cues that fit, preferring commas and
    pauses near the middle."""
    if len(ws) == 1 or fits(ws):
        return [ws]
    total = len(text_of(ws))
    best, best_score = 1, float("inf")
    pos = 0
    for i in range(1, len(ws)):
        pos += len(ws[i - 1]["text"]) + 1
        score = abs(pos - total / 2) / total          # 0 = perfect balance
        if ws[i - 1]["text"].endswith(CLAUSE_END):
            score -= 0.25
        score -= min(ws[i]["start"] - ws[i - 1]["end"], 0.5) * 0.4
        if score < best_score:
            best, best_score = i, score
    return split_long(ws[:best]) + split_long(ws[best:])


def wrap(text):
    """Wrap into at most MAX_LINES balanced lines, or None if it can't fit."""
    if len(text) <= LINE_CHAR_LIMIT:
        return text
    tokens = text.split(" ")
    best, best_score = None, float("inf")
    for i in range(1, len(tokens)):
        a, b = " ".join(tokens[:i]), " ".join(tokens[i:])
        if len(a) > LINE_CHAR_LIMIT or len(b) > LINE_CHAR_LIMIT:
            continue
        score = abs(len(a) - len(b)) - (8 if a.endswith(CLAUSE_END) else 0)
        if score < best_score:
            best, best_score = a + "\n" + b, score
    return best


def fmt_time(s):
    ms = int(round(s * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    sec, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{sec:02},{ms:03}"


def build_cues(words):
    cues = []
    for sentence in split_sentences(words):
        cues.extend(split_long(sentence))
    # Start each cue LEAD_IN early, but never before the previous cue's last word.
    starts = [max(ws[0]["start"] - LEAD_IN,
                  cues[i - 1][-1]["end"] + 0.05 if i else 0.0, 0.0)
              for i, ws in enumerate(cues)]
    starts = [min(s, ws[0]["start"]) for s, ws in zip(starts, cues)]
    out = []
    for i, ws in enumerate(cues):
        start, end = starts[i], ws[-1]["end"]
        limit = starts[i + 1] - 0.05 if i + 1 < len(cues) else end + END_PAD
        end = min(max(end + END_PAD, start + MIN_CUE_SECONDS), max(limit, end))
        text = wrap(text_of(ws)) or text_of(ws)
        out.append((start, end, text))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("json_file")
    ap.add_argument("-o", "--output", help="Output stem (default: JSON basename).")
    args = ap.parse_args()

    cues = build_cues(load_words(args.json_file))
    stem = args.output or os.path.splitext(args.json_file)[0]
    out_path = f"{stem}.srt"
    with open(out_path, "w", encoding="utf-8") as f:
        for n, (start, end, text) in enumerate(cues, 1):
            f.write(f"{n}\n{fmt_time(start)} --> {fmt_time(end)}\n{text}\n\n")
    print(f"Wrote {out_path} ({len(cues)} cues)")


if __name__ == "__main__":
    main()
