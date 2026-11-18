"""kumapie's Japanese word lists to import as known (Settings, onboarding), from jiten-lexicon.tsv.gz's JMdict tags.

jiten.moe's word sets need a sign-in since 2026-11 (HTTP 401), so kumapie publishes its own in its public repo
(kumapie-tv/wordlists/ja.tsv, read by lang/.../Jiten.kt from raw.githubusercontent.com). A word is in a list when
any of its senses has one of the list's tags, as Jiten builds its sets (WordSetCommands.CreateWordSetFromPartOfSpeech);
every written form of the word goes in.

  .venv\\Scripts\\python.exe tools\\kumapie\\wordlists.py [lexicon] [out]
"""
import gzip
import sys
from pathlib import Path

LEXICON = Path(r"C:\dojo\state\dicts\jiten-lexicon.tsv.gz")
OUT = Path(r"C:\dojo\kumapie-tv\wordlists\ja.tsv")

# slug -> JMdict tags. The slugs are the app's (Jiten.SETS); keep them when changing a list.
LISTS = {
    "particles": {"prt"},
    "counters": {"ctr"},
    "onomatopoeia": {"on-mim"},
    "grammar": {"aux", "aux-v", "aux-adj", "cop", "conj", "pn", "pref", "suf", "n-suf"},
    "honorific": {"hon", "hum", "pol"},
    "interjections": {"int"},
}


def build(lexicon: Path) -> dict[str, set[str]]:
    out = {slug: set() for slug in LISTS}
    with gzip.open(lexicon, "rt", encoding="utf-8") as f:
        for line in f:
            if not line.startswith("W\t"):
                continue
            cols = line.rstrip("\n").split("\t")
            tags = set(cols[2].split(","))
            forms = [form.split(":")[0][1:] for form in cols[6].split(";") if form]
            for slug, want in LISTS.items():
                if tags & want:
                    out[slug].update(forms)
    return out


def main() -> None:
    lexicon = Path(sys.argv[1]) if len(sys.argv) > 1 else LEXICON
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else OUT
    lists = build(lexicon)
    for slug, words in lists.items():
        assert words, f"{slug}: empty (lexicon format changed?)"
    assert "は" in lists["particles"] and "ページ" in lists["counters"], "spot check failed"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        f.write("# kumapie's Japanese word lists: list<TAB>written form. From JMdict (EDRDG, CC BY-SA 4.0) tags,\n")
        f.write("# built by kumapie-server tools/kumapie/wordlists.py. Edit there, not here.\n")
        for slug, words in lists.items():
            f.writelines(f"{slug}\t{w}\n" for w in sorted(words))
    print(out, {slug: len(words) for slug, words in lists.items()})


if __name__ == "__main__":
    main()
