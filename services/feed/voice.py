"""The generated voice for word audio when no person's recording exists (Pedro's picks, 2026-10-24):

  ja  VOICEVOX ずんだもん (the engine in apps\\voicevox, started here on the CPU when it isn't running)
  de  Piper Thorsten (high), through sherpa-onnx
  en  Kokoro Michael, through sherpa-onnx

Models in state\\tts\\. say() writes a WAV; the caller turns it into mp3. Credit "VOICEVOX:ずんだもん" if shared.
"""
from __future__ import annotations

import json
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import wave
from pathlib import Path

from dojo import cfg

MODELS = cfg.state / "tts"
VOICEVOX = cfg.root / "apps" / "voicevox" / "engine" / "windows-directml" / "run.exe"
VOICEVOX_URL = "http://127.0.0.1:50021"
ZUNDAMON = 3  # ずんだもん ノーマル
MICHAEL = 6  # kokoro-en-v0_19: am_michael

_engines: dict[str, object] = {}
_lock = threading.Lock()


def _sherpa(lang: str):
    import sherpa_onnx as s  # noqa: PLC0415 - only the voice needs it

    if lang == "de":
        d = MODELS / "vits-piper-de_DE-thorsten-high"
        model = s.OfflineTtsModelConfig(vits=s.OfflineTtsVitsModelConfig(
            model=str(d / "de_DE-thorsten-high.onnx"), tokens=str(d / "tokens.txt"), data_dir=str(d / "espeak-ng-data")),
            num_threads=2)
    else:
        d = MODELS / "kokoro-en-v0_19"
        model = s.OfflineTtsModelConfig(kokoro=s.OfflineTtsKokoroModelConfig(
            model=str(d / "model.onnx"), voices=str(d / "voices.bin"), tokens=str(d / "tokens.txt"),
            data_dir=str(d / "espeak-ng-data")), num_threads=2)
    return s.OfflineTts(s.OfflineTtsConfig(model=model, max_num_sentences=1))


def _write(path: Path, samples, rate: int) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(int(max(-1.0, min(1.0, x)) * 32767).to_bytes(2, "little", signed=True) for x in samples))


def _voicevox_up() -> bool:
    try:
        urllib.request.urlopen(VOICEVOX_URL + "/version", timeout=2).read()
        return True
    except OSError:
        pass
    if not VOICEVOX.is_file():
        return False
    # ponytail: started once and left running (~1 GB RAM, CPU mode); a stopped engine is started again at the next word
    subprocess.Popen([str(VOICEVOX), "--host", "127.0.0.1", "--port", "50021"], cwd=str(VOICEVOX.parent),
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    for _ in range(60):
        time.sleep(1)
        try:
            urllib.request.urlopen(VOICEVOX_URL + "/version", timeout=2).read()
            return True
        except OSError:
            pass
    return False


def _voicevox(text: str, out: Path) -> bool:
    if not _voicevox_up():
        return False
    q = urllib.parse.urlencode({"text": text, "speaker": ZUNDAMON})
    post = lambda path, data=None: urllib.request.urlopen(urllib.request.Request(  # noqa: E731
        f"{VOICEVOX_URL}{path}", data=data or b"", method="POST", headers={"Content-Type": "application/json"}), timeout=60).read()
    query = json.loads(post(f"/audio_query?{q}"))
    query["postPhonemeLength"] = 0.2
    out.write_bytes(post(f"/synthesis?speaker={ZUNDAMON}", json.dumps(query).encode()))
    return out.stat().st_size > 44


def say(text: str, lang: str, out: Path) -> bool:
    """[text] said by the language's voice into the WAV [out]; False when there is no voice for it."""
    with _lock:
        if lang == "ja":
            return _voicevox(text, out)
        if lang not in ("de", "en"):
            return False
        if lang not in _engines:
            _engines[lang] = _sherpa(lang)
        a = _engines[lang].generate(text, sid=MICHAEL if lang == "en" else 0, speed=1.0)
    if not len(a.samples):
        return False
    _write(out, a.samples, a.sample_rate)
    return True


if __name__ == "__main__":
    import sys
    import tempfile

    for lang, word in (("de", "Brötchen"), ("en", "squirrel"), ("ja", "箸")):
        f = Path(tempfile.mkdtemp()) / f"{lang}.wav"
        ok = say(word, lang, f)
        print(lang, word, ok, f.stat().st_size if ok else 0)
        assert ok or (lang == "ja" and "--no-ja" in sys.argv)
    print("ok")
