"""Japanese words on the PC exactly as kumapie makes them on the device: Jiten's splitter, ported to Kotlin in kumapie's
:lang (kumapie_plan.md item 26b), run through jiten_split.ps1 (gradlew :lang:jitenSplit). parsed() gives per text
[[surface, offset, key or None, None]], the parsed_json newepisode.build takes; the key is the word's dictionary form."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).with_name("jiten_split.ps1")


def parsed(texts: list[str]) -> list[list]:
    with tempfile.TemporaryDirectory() as tmp:
        src, out = Path(tmp) / "texts.json", Path(tmp) / "words.json"
        src.write_text(json.dumps(texts, ensure_ascii=False), encoding="utf-8")
        subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT), "lines", str(src), str(out)],
                       check=True, stdout=sys.stderr)
        return json.loads(out.read_text(encoding="utf-8"))


if __name__ == "__main__":
    rows = parsed(["荷物が届いた。", "黒板消しは普通。", "でも防御力が高い"])
    words = [[r[0] for r in row if r[2]] for row in rows]
    assert words == [["荷物", "が", "届いた"], ["黒板消し", "は", "普通"], ["でも", "防御力", "が", "高い"]], words
    assert "".join(r[0] for r in rows[0]) == "荷物が届いた。"
    print("ok", words)
