"""
Free, local transcription with Parakeet TDT 0.6B v3 (onnx-asr, CPU) -> the canonical transcript JSON that
srt_spaced.py reads (same "words" shape as Soniox). European languages only (no Japanese). No speakers.

Runs in the aniworld venv (config.toml [aniworld], has onnx-asr), CPU only: the GPU is left alone for gaming.
    C:\\dojo\\apps\\aniworld\\Scripts\\python.exe pipeline\\parakeet.py -o <stem> <stem>.mp4
    .venv\\Scripts\\python.exe pipeline\\srt_spaced.py -o <stem> <stem>.json
"""

import argparse
import json
import os
import subprocess
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("media")
    ap.add_argument("-o", "--output", required=True, help="output stem (writes <stem>.json)")
    args = ap.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        wav = os.path.join(tmp, "audio.wav")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", args.media, "-vn", "-ac", "1", "-ar", "16000", wav],
                       check=True)
        model = (onnx_asr.load_model("nemo-parakeet-tdt-0.6b-v3", providers=["CPUExecutionProvider"])
                 .with_vad(onnx_asr.load_vad("silero"), max_speech_duration_s=20).with_timestamps())
        words = to_words(model.recognize(wav))

    with open(f"{args.output}.json", "w", encoding="utf-8") as f:
        json.dump({"provider": "parakeet-tdt-0.6b-v3", "words": words}, f, ensure_ascii=False)
    print(f"{args.output}.json: {sum(w['type'] == 'word' for w in words)} tokens")


if __name__ == "__main__":
    main()
