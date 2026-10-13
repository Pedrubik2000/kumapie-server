"""Build kumapie's Jiten word database ("lexicon") from JMdict, JMnedict and Jiten's frequency list.

kumapie_plan.md item 26b step 3. Jiten (github.com/Sirush/Jiten, Apache-2.0, commit 44b48a4) keeps JMdict in Postgres
and its parser preloads lookup keys, word metadata and frequency ranks (Parser.EnsureInitializedAsync,
Runtime/ParserRuntime.cs); kumapie's Kotlin port (lang/.../jiten/JitenLexicon.kt) reads this file instead. It
reproduces what Jiten's importer stores (Jiten.Core/Data/JMDict/JMDictHelper.cs, the Sync* paths it runs today):

- word id = JMdict ent_seq; JMnedict entries are merged by their first kanji form (else first reading) and get the
  smallest ent_seq of the group (SyncMissingJMNedict on a fresh database); Jiten's custom words 8000000-8000003.
- word parts of speech = every sense's <pos> (inherited from the previous sense when empty) + <misc>, distinct, as
  entity names (ElToPos: reverse map of the JMdict DTD, then Jiten's name-* entities, then the file's own entities).
- word priorities = every form's ke_pri/re_pri + "jiten" for Jiten's hand-picked ids; names get "name".
- forms: kanji forms then kana forms (ReadingIndex = position), ゎ/ヮ -> わ, with their priorities and info tags.
- senses: their tags and the reading indices they are restricted to (stagk/stagr); no glosses.
- lookups exactly as GenerateLookupsForForm: hiragana without long-vowel conversion, hiragana with it (WanaKana
  ToHiragana: オー -> おう), and the katakana text itself.
- fully archaic (every sense tagged arch), gairaigo origin (has <lsource>), frequency rank (see freq_ranks).

Skipped: glosses, furigana/ruby, pitch accents, Jiten's wago/kango origin CSV, its observed word frequencies
(WordFrequencyPriors) and per-form frequencies, xrefs. Licences: JMdict/JMnedict EDRDG CC BY-SA 4.0; the frequency
list is Jiten's (CC BY-SA 4.0).

Output (UTF-8 lines, gzip), sorted so the reader can binary-search without re-sorting:
  #...                                  comments (sources, counts)
  W <id> <pos> <pri> <flags> <rank> <forms> <senses>     one per word, ascending id, tab-separated
      pos, pri    comma-separated tags (may be empty)
      flags       a = fully archaic, g = gairaigo origin
      rank        frequency rank (1 = most frequent) or empty
      forms       ';'-separated: K|R + text [+ ':' + priorities + ':' + info tags + ':' + n (re_nokanji) or empty]
      senses      ';'-separated: tags (comma) [+ '@' + restricted reading indices (comma)]; empty = one sense
                  carrying the word's own tags (every name), so the reader keeps less text
  L <key> <ids>                          one per lookup key, ascending UTF-16 order, ids space-separated ascending

  .venv\\Scripts\\python.exe tools\\kumapie\\jiten_lexicon.py [out]   (default C:\\dojo\\work\\jiten-port\\lexicon)
"""
import gzip
import json
import re
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

WORK = Path(r"C:\dojo\work")
JMDICT = WORK / "jiten-port" / "jmdict" / "JMdict_e.gz"
JMNEDICT = WORK / "jiten-port" / "jmdict" / "JMnedict.xml.gz"
FREQ = WORK / "ja-dicts" / "1. Basic Setup" / "[Freq] Jiten (Global).zip"

# JMDictHelper.LoadEntities: added after the DTD's own entities (first expansion wins in the reverse map).
NAME_ENTITIES = [("name-char", "character"), ("name-company", "company name"), ("name-creat", "creature"),
                 ("name-dei", "deity"), ("name-doc", "document"), ("name-ev", "event"),
                 ("name-fem", "female given name or forename"), ("name-fict", "fiction"),
                 ("name-given", "given name or forename, gender not specified"), ("name-group", "group"),
                 ("name-leg", "legend"), ("name-masc", "male given name or forename"), ("name-myth", "mythology"),
                 ("name-obj", "object"), ("name-organization", "organization name"), ("name-oth", "other"),
                 ("name-person", "full name of a particular person"), ("name-place", "place name"),
                 ("name-product", "product name"), ("name-relig", "religion"), ("name-serv", "service"),
                 ("name-ship", "ship name"), ("name-station", "railway station"),
                 ("name-surname", "family or surname"), ("name-unclass", "unclassified name"),
                 ("name-work", "work of art, literature, music, etc. name")]

# Import/SyncJmDict custom priorities and words.
JITEN_PRIORITY_IDS = {
    1332650, 2848543, 1160790, 1203260, 1397260, 1499720, 1315130, 1550190, 1191730, 2844190, 2207630, 1442490,
    1423310, 1502390, 1343100, 1610040, 2059630, 1495580, 1288850, 1392580, 1511350, 1648450, 1534790, 2105530,
    1223615, 1421850, 1020650, 1310640, 1495770, 1375610, 1334590, 1609980, 1579260, 1351580, 1983760, 1207510,
    1266890, 1163940, 1625330, 1416220, 1356690, 2020520, 2084840, 2603500, 1522150, 1591970, 1920245, 1177490,
    1582430, 1310670, 1577120, 1352570, 1604800, 1581310, 2720360, 1318950, 2541230, 1288500, 1121740, 1074630,
    1111330, 1116190, 2815290, 1157170, 2855934, 1245290, 1075810, 1314600, 1020910, 1430230, 1349380, 1347580,
    1311110, 1154770, 1282790, 1478060, 2068450, 1169250, 1598460, 1144510, 1282970, 1982860, 1609715,
    5060001, 8000003}

ENTITY_RE = re.compile(r'<!ENTITY (.*) "(.*)">')


def entities(xml_gz, reverse):
    """LoadEntities' header scan: ENTITY lines before the first element, first expansion wins."""
    with gzip.open(xml_gz, "rt", encoding="utf-8") as f:
        for line in f:
            if line.startswith("<") and not line.startswith("<!") and not line.startswith("<?"):
                break
            m = ENTITY_RE.search(line)
            if m:
                reverse.setdefault(m.group(2), m.group(1))
    return reverse


# --- WanaKana subset (WanaKana-net semantics for kana/kanji input; the dictionaries hold no romaji) ---
LONG_VOWEL = {}
for chars, v in (("あかさたなはまやらわがざだばぱぁゃゎ", "あ"), ("いきしちにひみりぎじぢびぴぃゐ", "い"),
                 ("うくすつぬふむゆるぐずづぶぷぅゅゔ", "う"), ("えけせてねへめれげぜでべぺぇゑ", "え"),
                 ("おこそとのほもよろをごぞどぼぽぉょ", "う")):
    for c in chars:
        LONG_VOWEL[c] = v


class NoVowel(Exception):
    """WanaKana's chōonpu expansion has no vowel for the preceding kana (ッー, ンー)."""


def is_char_katakana(c):
    return 0x30A1 <= ord(c) <= 0x30FC


def is_char_hiragana(c):
    return c == "ー" or 0x3041 <= ord(c) <= 0x3096


def is_katakana(s):
    return bool(s) and all(is_char_katakana(c) for c in s)


def katakana_to_hiragana(s, convert_long_vowel):
    out, prev = [], ""
    for i, c in enumerate(s):
        if c == "・" or (c == "ー" and i < 1) or c in "ヶヵ":
            out.append(c)
        elif convert_long_vowel and prev and c == "ー":
            v = LONG_VOWEL.get(prev)
            if v is None:
                raise NoVowel(s)
            out.append(v)
        elif c != "ー" and is_char_katakana(c):
            prev = chr(ord(c) - 0x60)
            out.append(prev)
        else:
            prev = ""
            out.append(c)
    return "".join(out)


def lookup_keys(text, stats):
    """GenerateLookupsForForm. A chōonpu WanaKana can't expand would have thrown; keep the unconverted key."""
    t = text.replace("ゎ", "わ").replace("ヮ", "わ")
    key = katakana_to_hiragana(t, False)
    keys = [key]
    try:
        no_lvm = katakana_to_hiragana(t, True)
    except NoVowel:
        stats["no_vowel"] = stats.get("no_vowel", 0) + 1
        no_lvm = key
    if no_lvm != key:
        keys.append(no_lvm)
    if is_katakana(text):
        keys.append(text)
    return keys


def fix(t):
    return (t or "").replace("ゎ", "わ").replace("ヮ", "わ")


def uniq(xs):
    return list(dict.fromkeys(xs))


def parse_jmdict(reverse):
    el = lambda e: reverse.get(e.text or "", e.text or "")
    words = {}
    for _, e in ET.iterparse(gzip.open(JMDICT), events=("end",)):
        if e.tag != "entry":
            continue
        wid = int(e.findtext("ent_seq"))
        forms = []  # [type, text, priorities, info, nokanji]
        for k in e.findall("k_ele"):
            forms.append(["K", fix(k.findtext("keb")), [p.text for p in k.findall("ke_pri")],
                          [el(x) for x in k.findall("ke_inf")], False])
        for r in e.findall("r_ele"):
            forms.append(["R", fix(r.findtext("reb")), [p.text for p in r.findall("re_pri")],
                          [el(x) for x in r.findall("re_inf")], r.find("re_nokanji") is not None])
        kanji_idx, kana_idx = {}, {}
        for i, f in enumerate(forms):
            (kanji_idx if f[0] == "K" else kana_idx).setdefault(f[1], i)
        senses, inherited = [], []
        for s in e.findall("sense"):
            pos = [el(x) for x in s.findall("pos")] or list(inherited)
            inherited = pos
            misc = [el(x) for x in s.findall("misc")]
            restr = sorted({kanji_idx[x.text] for x in s.findall("stagk") if x.text in kanji_idx}
                           | {kana_idx[x.text] for x in s.findall("stagr") if x.text in kana_idx})
            has_eng = any(g.get("{http://www.w3.org/XML/1998/namespace}lang", "eng") == "eng"
                          for g in s.findall("gloss"))
            senses.append([uniq(pos + misc), restr, has_eng])
        words[wid] = {
            "pos": uniq(t for s in senses for t in s[0]),
            "pri": uniq(p for f in forms for p in f[2]),
            "forms": forms, "senses": senses,
            "gairaigo": any(s.find("lsource") is not None for s in e.findall("sense")) or e.find("lsource") is not None,
        }
        e.clear()
    return words


def parse_jmnedict(reverse, jmdict_ids):
    """SyncMissingJMNedict phase 1: ParseNameKEle/REle/Trans, merged by first keb (else first reading). Entries whose
    ent_seq JMdict already has (~7 300 names moved into JMdict, e.g. ２ちゃんねる) are skipped as ImportJMNedict does."""
    el = lambda e: reverse.get(e.text or "", e.text or "")
    groups, members = {}, {}
    for _, e in ET.iterparse(gzip.open(JMNEDICT), events=("end",)):
        if e.tag != "entry":
            continue
        wid = int(e.findtext("ent_seq"))
        forms, pri, defs = [], [], []
        for k in e.findall("k_ele"):
            forms.append(["K", k.findtext("keb") or ""])
            pri += [p.text for p in k.findall("ke_pri")]
        primary = forms[0][1] if forms else None
        for r in e.findall("r_ele"):
            restr = [x.text for x in r.findall("re_restr")]
            obsolete = any((x.text or "").lower() == "&ok" for x in r.findall("re_inf"))
            pri += [p.text for p in r.findall("re_pri")]
            if (not restr or any(f[1] in restr for f in forms)) and not obsolete:
                forms.append(["R", r.findtext("reb") or ""])
        for t in e.findall("trans"):
            types = [el(x) for x in t.findall("name_type")] or ["name"]
            meanings = [x.text for x in t.findall("trans_det")]
            if meanings:
                defs.append((tuple(types), tuple(meanings)))
        e.clear()
        for f in forms:
            f[1] = fix(f[1])
        if wid >= 8000000 or not forms or wid in jmdict_ids:
            continue
        key = primary if primary is not None else forms[0][1]
        members.setdefault(key, []).append(wid)
        g = groups.get(key)
        if g is None:
            groups[key] = {"forms": forms, "defs": defs, "pri": uniq(pri)}
        else:  # MergeNameEntries
            for f in forms:
                if not any(x[1] == f[1] for x in g["forms"]):
                    g["forms"].append(f)
            for d in defs:
                if d not in g["defs"]:
                    g["defs"].append(d)
            for p in pri:
                if p not in g["pri"]:
                    g["pri"].append(p)
    words = {}
    for key, g in groups.items():
        wid = min(members[key])
        if wid in words or not g["defs"]:
            continue  # first group wins a collapsed id; BuildNewNameWord rejects words without definitions
        words[wid] = {
            "pos": uniq(t for d in g["defs"] for t in d[0]),
            "pri": uniq(g["pri"] + ["name"]),
            "forms": [[f[0], f[1], [], [], False] for f in g["forms"]],
            "senses": [[list(d[0]), [], True] for d in g["defs"]],
            "gairaigo": False,
        }
    return words


def custom_words(words):
    """GetCustomWords plus the custom senses/POS of Import/SyncJmDict."""
    def word(forms, pos, pri=()):
        return {"pos": list(pos), "pri": list(pri), "forms": [[t, x, [], [], False] for t, x in forms],
                "senses": [[list(pos), [], True]], "gairaigo": False}
    words[8000000] = word([("R", "でした")], ["exp"])
    words[8000001] = word([("R", "イクシオトキシン")], ["n"])
    words[8000002] = word([("K", "逢魔"), ("R", "おうま")], ["exp"])
    words[8000003] = word([("R", "こうする")], ["exp", "vs-i"])
    if 2029110 in words:
        w = words[2029110]
        w["senses"].append([["prt"], [], True])
        w["pos"] = uniq(w["pos"] + ["prt"])
    if 1524610 in words and "n" not in words[1524610]["pos"]:
        words[1524610]["pos"].append("n")
    if 5141615 in words:  # ImportJMNedict: 駅前通 also a plain noun
        w = words[5141615]
        w["senses"].append([["n"], [], True])
        if "n" not in w["pos"]:
            w["pos"].append("n")
    for wid in JITEN_PRIORITY_IDS:
        if wid in words and "jiten" not in words[wid]["pri"]:
            words[wid]["pri"].append("jiten")


def freq_ranks(words):
    """Jiten's global list (Yomitan, rank-based) to word ids. A row is one (form, reading) of a word: rows with a reading
    match words having that kanji form and kana form; kana-only rows prefer kana-primary words (no kanji form or uk),
    so 野 doesn't take の's rank. A word keeps its best rank. Jiten's WordFrequencies table is per word id; this is the
    closest public copy of it."""
    by_form = {}
    for wid, w in words.items():
        for f in w["forms"]:
            by_form.setdefault(f[1], []).append(wid)
    kana_forms = {wid: {f[1] for f in w["forms"] if f[0] == "R"} for wid, w in words.items()}
    kana_primary = {wid for wid, w in words.items()
                    if "uk" in w["pos"] or not any(f[0] == "K" for f in w["forms"])}
    ranks, rows, unmatched = {}, 0, 0
    with zipfile.ZipFile(FREQ) as z:
        for name in sorted(n for n in z.namelist() if n.startswith("term_meta_bank_")):
            for term, kind, data in json.loads(z.read(name)):
                if kind != "freq":
                    continue
                rows += 1
                reading = data.get("reading") if isinstance(data, dict) else None
                f = data.get("frequency", data) if isinstance(data, dict) else data
                value = f.get("value") if isinstance(f, dict) else f
                ids = by_form.get(term, [])
                if reading and reading != term:
                    ids = [i for i in ids if reading in kana_forms[i]]
                else:
                    ids = [i for i in ids if i in kana_primary] or ids
                if not ids:
                    unmatched += 1
                for i in ids:
                    if value < ranks.get(i, 1 << 30):
                        ranks[i] = value
    return ranks, rows, unmatched


def fully_archaic(w):
    """LoadFullyArchaicWordIds: tagged arch and every English sense tagged arch."""
    eng = [s for s in w["senses"] if s[2]]
    return "arch" in w["pos"] and bool(eng) and all("arch" in s[0] for s in eng)


def main():
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else WORK / "jiten-port" / "lexicon"
    out_dir.mkdir(parents=True, exist_ok=True)
    jm_reverse = entities(JMDICT, {})
    words = parse_jmdict(jm_reverse)
    n_jmdict = len(words)
    name_reverse = dict(jm_reverse)
    for k, v in NAME_ENTITIES:
        name_reverse.setdefault(v, k)
    names = parse_jmnedict(entities(JMNEDICT, name_reverse), words.keys())
    words.update(names)
    custom_words(words)
    ranks, freq_rows, freq_unmatched = freq_ranks(words)

    stats = {}
    lookups = {}
    for wid, w in words.items():
        for f in w["forms"]:
            for k in lookup_keys(f[1], stats):
                lookups.setdefault(k, set()).add(wid)

    sep = re.compile(r"[\t\n;:,|@]")
    path = out_dir / "jiten-lexicon.tsv.gz"
    archaic = 0
    with gzip.open(path, "wt", encoding="utf-8", newline="\n", compresslevel=9) as out:
        out.write("#jiten-lexicon 1\n#JMdict/JMnedict (EDRDG, CC BY-SA 4.0), Jiten global frequency list "
                  "(jiten.moe, CC BY-SA 4.0); built by kumapie tools/kumapie/jiten_lexicon.py\n")
        out.write(f"#words {len(words)} lookups {len(lookups)}\n")
        for wid in sorted(words):
            w = words[wid]
            flags = ""
            if fully_archaic(w):
                flags += "a"
                archaic += 1
            if w["gairaigo"]:
                flags += "g"
            forms = []
            for t, text, pri, info, nokanji in w["forms"]:
                assert not sep.search(text), (wid, text)
                extra = f":{','.join(pri)}:{','.join(info)}:{'n' if nokanji else ''}"
                forms.append(t + text + (extra if extra != ":::" else ""))
            senses = [",".join(s[0]) + ("@" + ",".join(map(str, s[1])) if s[1] else "") for s in w["senses"]]
            if senses == [",".join(w["pos"])]:
                senses = []
            out.write(f"W\t{wid}\t{','.join(w['pos'])}\t{','.join(w['pri'])}\t{flags}\t{ranks.get(wid, '')}"
                      f"\t{';'.join(forms)}\t{';'.join(senses)}\n")
        for key in sorted(lookups, key=lambda s: s.encode("utf-16-be")):
            out.write(f"L\t{key}\t{' '.join(map(str, sorted(lookups[key])))}\n")

    name_tags = {}
    for w in names.values():
        for t in w["pos"]:
            name_tags[t] = name_tags.get(t, 0) + 1
    print(f"words: {len(words)} (JMdict {n_jmdict}, JMnedict groups {len(names)}, custom 4)")
    print(f"lookup keys: {len(lookups)}, word-key pairs: {sum(len(v) for v in lookups.values())}, "
          f"chōonpu without a vowel: {stats.get('no_vowel', 0)}")
    print(f"frequency rows: {freq_rows}, unmatched: {freq_unmatched}, words with a rank: {len(ranks)}")
    print(f"fully archaic: {archaic}; JMnedict name tags: {dict(sorted(name_tags.items(), key=lambda x: -x[1]))}")
    print(f"{path}: {path.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
