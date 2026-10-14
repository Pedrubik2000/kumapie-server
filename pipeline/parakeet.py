"""
Free, local transcription with Parakeet TDT 0.6B v3 (onnx-asr, CPU) -> the canonical transcript JSON that
srt_spaced.py reads (same "words" shape as Soniox). European languages only (no Japanese). No speakers.

Runs in the aniworld venv (config.toml [aniworld], has onnx-asr), CPU only: the GPU is left alone for gaming.
    C:\\dojo\\apps\\aniworld\\Scripts\\python.exe pipeline\\parakeet.py -o <stem> <stem>.mp4
    .venv\\Scripts\\python.exe pipeline\\srt_spaced.py -o <stem> <stem>.json
Japanese (--ja <model dir>): NVIDIA's Parakeet TDT-CTC 0.6B ja (CTC head, int8, sherpa-onnx; the folder holds
model.int8.onnx, tokens.txt and silero_vad.onnx) -> <stem>.srt directly, lines cut at sentence marks and pauses, at most
8 s (as kumapie's Parakeet ja on the phone).
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile

import onnx_asr

MAX_TOKEN_SECONDS = 0.6  # a token lasts until the next one, at most this (so pauses stay pauses)


def to_words(segments):
    """Parakeet's segment-relative sub-word tokens -> Soniox-style word/spacing tokens with absolute times."""
    out = []
    for seg in segments:
        times = [seg.start + t for t in seg.timestamps] + [seg.end]
        for i, tok in enumerate(seg.tokens):
            if tok.startswith(" ") and out:
                out.append({"type": "spacing", "text": " "})
            text = tok.lstrip(" ")
            if text:
                end = min(times[i + 1], times[i] + MAX_TOKEN_SECONDS)
                out.append({"type": "word", "text": text, "start": round(times[i], 3),
                            "end": round(end, 3), "speaker_id": "1"})
        out.append({"type": "spacing", "text": " "})
    return out


def split_lines(tokens, times, end):
    """One stretch of speech -> subtitle lines (start, end, text): a line ends at a sentence mark or a pause once it
    is long enough; past 7.5 s at its longest pause (Silero doesn't cut long speech by itself). Same rule as
    kumapie's Parakeet.kt."""
    n = len(tokens)
    after = lambda i: times[i + 1] if i + 1 < n else end  # noqa: E731 - when the next token starts
    gap = lambda i: after(i) - times[i]  # noqa: E731 - this token and the silence after it
    # Never before hiragana (誤|った, 育|つ: the word's ending); best right after a particle.
    free = lambda i: i + 1 < n and not ("ぁ" <= tokens[i + 1].strip()[:1] <= "ゖ" or tokens[i + 1].strip()[:1] in "ーっ")  # noqa: E731
    score = lambda i: gap(i) + (1 if tokens[i].strip()[-1:] in "はがをにでともへや" else 0)  # noqa: E731
    out, first, i = [], 0, 0
    while i < n:
        dur, tok = after(i) - times[first], tokens[i].strip()
        cut = i + 1 == n or (dur >= 2.5 and tok in ("。", "？", "！", "?", "!")) \
            or (dur >= 3.5 and (tok == "、" or (free(i) and gap(i) >= 0.75)))
        if not cut and dur >= 7.5:  # no good place yet: the best one after the first 2 s
            ok = [j for j in range(first, i + 1) if times[j] - times[first] >= 2 and (free(j) or tokens[j].strip() == "、")]
            i = max(ok, key=score, default=i)
            cut = True
        if cut:
            text = "".join(tokens[first:i + 1]).strip()
            if text:
                out.append((times[first], after(i), text))
            first = i + 1
        i += 1
    return out


def japanese(wav, model_dir, out):
    import sherpa_onnx
    import soundfile
    audio = soundfile.read(wav, dtype="float32")[0]
    rec = sherpa_onnx.OfflineRecognizer.from_nemo_ctc(model=os.path.join(model_dir, "model.int8.onnx"),
                                                      tokens=os.path.join(model_dir, "tokens.txt"), num_threads=4)
    vad = sherpa_onnx.VoiceActivityDetector(sherpa_onnx.VadModelConfig(sherpa_onnx.SileroVadModelConfig(
        model=os.path.join(model_dir, "silero_vad.onnx"), min_silence_duration=0.3, min_speech_duration=0.25,
        max_speech_duration=20), sample_rate=16000), buffer_size_in_seconds=60)

    def stamp(t):
        ms = round(t * 1000)
        return f"{ms // 3600000:02}:{ms // 60000 % 60:02}:{ms // 1000 % 60:02},{ms % 1000:03}"

    lines = []

    def drain():
        while not vad.empty():
            seg = vad.front
            s = rec.create_stream()
            s.accept_waveform(16000, seg.samples)
            rec.decode_stream(s)
            start = seg.start / 16000
            lines.extend(split_lines(s.result.tokens, [start + t for t in s.result.timestamps],
                                     start + len(seg.samples) / 16000))
            vad.pop()

    for i in range(0, len(audio), 512):
        vad.accept_waveform(audio[i:i + 512])
        drain()
    vad.flush()
    drain()
    with open(out, "w", encoding="utf-8") as f:
        f.writelines(f"{n}\n{stamp(a)} --> {stamp(b)}\n{t}\n\n" for n, (a, b, t) in enumerate(lines, 1))
    print(f"{out}: {len(lines)} lines")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("media")
    ap.add_argument("-o", "--output", required=True, help="output stem (writes <stem>.json)")
    ap.add_argument("--ja", metavar="MODEL_DIR", help="Japanese: Parakeet ja (sherpa-onnx) in this folder -> <stem>.srt")
    args = ap.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        wav = os.path.join(tmp, "audio.wav")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", args.media, "-vn", "-ac", "1", "-ar", "16000", wav],
                       check=True)
        if args.ja:
            return japanese(wav, args.ja, f"{args.output}.srt")
        model = (onnx_asr.load_model("nemo-parakeet-tdt-0.6b-v3", providers=["CPUExecutionProvider"])
                 .with_vad(onnx_asr.load_vad("silero"), max_speech_duration_s=20).with_timestamps())
        words = to_words(model.recognize(wav))

    with open(f"{args.output}.json", "w", encoding="utf-8") as f:
        json.dump({"provider": "parakeet-tdt-0.6b-v3", "words": words}, f, ensure_ascii=False)
    print(f"{args.output}.json: {sum(w['type'] == 'word' for w in words)} tokens")


if __name__ == "__main__":
    if sys.argv[1:] == ["--check"]:  # split_lines' rule
        toks = list("今日は晴れ。") + list("明日は雨です") + ["、"] + list("でも大丈夫")
        lines = split_lines(toks, [i * 0.5 for i in range(len(toks))], len(toks) * 0.5)
        assert [t for _, _, t in lines] == ["今日は晴れ。", "明日は雨です、", "でも大丈夫"], lines
        assert all(b - a <= 8 for a, b, _ in split_lines(list("あ" * 40), [i * 0.3 for i in range(40)], 12))
        sys.exit(print("ok"))
    main()
