"""kumapie's Discover page: browse Stremio's catalog, pick a Torrentio + Real-Debrid file, watch it streamed
(kumapie_plan.md item 5b). Japanese first: Kitsu's catalog (the anime add-on Stremio uses).

The video never lands on the PC: the phone or TV plays Real-Debrid's link itself. The PC only makes the episode's words:
it reads the file once over the network for its Japanese subtitle track, else its audio + English track to fit Jimaku's
subtitles (jobs._jimaku: alass against the English track or a Parakeet transcript), then tv.stream() splits them like
any NuvioTV stream (a stream-<hex> episode, not listed in Shows). Asking again for the same file is instant.

  GET  /api/discover/catalog?q=<text>                 [{"id", "type", "name", "poster", "year"}] (no q: trending)
  GET  /api/discover/meta/<type>/<id>                 {"name", "poster", "background", "description", "year",
                                                       "episodes": [{"id", "season", "number", "title", "thumb"}]}
  GET  /api/discover/streams/<type>/<id>              [{"q", "label", "about", "hash", "file", "idx"}] best first
  POST /api/tv/watch {"person", "lang", "type", "id", "hash", "file", "idx", "title"?}
       -> {"state": "ready", "id": episode, "url": video} or {"state": "running" | "failed", "step", "error"}
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import urllib.parse
from pathlib import Path

from dojo import cfg
import jobs
import realdebrid
import stremio
import sync
from tv import STREAM

KITSU = stremio.META["kitsu"]
DONE = cfg.state / "feed" / "watch.json"  # {"<person>|<lang>|<hash>|<file>": stream episode id}
TEXT_SUBS = {"subrip": "srt", "ass": "ass", "ssa": "ass", "webvtt": "vtt", "mov_text": "srt"}
LANGS = {"ja": ("ja", "jpn", "jp"), "en": ("en", "eng")}
_lock = threading.Lock()
_running: dict[str, dict] = {}


def _json(url: str):
    with stremio._get(url) as r:
        return json.load(r)


def catalog(q: str = "") -> list[dict]:
    path = f"/catalog/anime/kitsu-anime-list/search={urllib.parse.quote(q)}.json" if q.strip() \
        else "/catalog/anime/kitsu-anime-trending.json"
    return [{"id": m["id"], "type": "movie" if m.get("type") == "movie" else "series", "name": m.get("name", ""),
             "poster": m.get("poster", ""), "year": str(m.get("releaseInfo") or m.get("year") or "")}
            for m in _json(KITSU + path).get("metas", []) if stremio.ID.match(m.get("id", ""))]


def meta(type_: str, sid: str) -> dict:
    m = _json(f"{KITSU if sid.startswith('kitsu:') else stremio.META['tt']}/meta/{type_}/{sid}.json").get("meta") or {}
    eps = [{"id": v["id"], "season": v.get("season") or 1, "number": v.get("episode") or v.get("number") or 0,
            "title": v.get("title") or v.get("name") or "", "thumb": v.get("thumbnail") or ""}
           for v in m.get("videos") or [] if stremio.ID.match(v.get("id", "")) and v.get("season", 1) != 0]
    eps.sort(key=lambda e: (e["season"], e["number"]))
    return {"name": m.get("name") or sid, "poster": m.get("poster") or "", "background": m.get("background") or "",
            "description": m.get("description") or "", "year": str(m.get("releaseInfo") or m.get("year") or ""),
            "episodes": eps}


def streams(type_: str, sid: str) -> list[dict]:
    """The best stremio.OPTIONS files per quality."""
    minutes = stremio._minutes(type_, sid)
    found = stremio.candidates(type_, sid, os.environ.get("RD_API_TOKEN", ""))
    out = []
    for q in stremio.QUALITIES:
        best = {}
        # Streamed to a phone: on Real-Debrid already, then up to 20 Mbps (a Remux's 45 stutters over mobile data),
        # then the highest bitrate; AV1 last (the TV can't play it).
        for c in sorted([c for c in found if q in stremio.qualities(c)], key=lambda c: (
                c["cached"] is not True, stremio._mbps(c, minutes) > 20, "AV1" in stremio.tags(c), -stremio._mbps(c, minutes))):
            best.setdefault(c["file"], c)
        for c in list(best.values())[:stremio.OPTIONS]:
            gb = c["gb"]
            size = f"{gb:.1f} GB" if gb >= 1 else f"{gb * 1000:.0f} MB"
            note = "" if c["cached"] else " · ⏳ Real-Debrid downloads it first" if c["cached"] is False else " · ⏳ maybe not on Real-Debrid"
            out.append({"q": q, "label": f"{q} · {size} · {stremio._mbps(c, minutes):.1f} Mbps{note}",
                        "about": stremio.about(c).strip(), "hash": c["hash"], "file": c["file"], "idx": c["idx"]})
    return out


def _done() -> dict:
    try:
        return json.loads(DONE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _direct(token: str, h: str, file: str, idx: int, step) -> str:
    """The file's playable https link: Real-Debrid's link for it (stremio._rd_link) unrestricted."""
    return realdebrid._call(token, "POST", "/unrestrict/link", {"link": stremio._rd_link(token, h, file, idx, step)})["download"]


def _tracks(url: str) -> list[dict]:
    out = jobs._out([jobs.FFPROBE, "-v", "error", "-show_entries", "stream=index,codec_type,codec_name:stream_tags=language",
                     "-of", "json", url])
    return json.loads(out).get("streams", [])


def _sub(tracks: list[dict], lang: str) -> dict | None:
    return next((t for t in tracks if t.get("codec_type") == "subtitle" and t.get("codec_name") in TEXT_SUBS
                 and (t.get("tags") or {}).get("language", "").lower() in LANGS[lang]), None)


def subtitles(url: str, show: str, season, number, file: str, tmp: Path, step) -> tuple[str, str]:
    """(Japanese subtitle file text, English or "") for the video at [url]: its own Japanese track, else Jimaku's
    fitted to its English track or its audio. One read of the whole file (ffmpeg)."""
    step("reading the file's tracks")
    tracks = _tracks(url)
    ja, en = _sub(tracks, "ja"), _sub(tracks, "en")
    cmd = [jobs.FFMPEG, "-y", "-v", "error", "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
           "-i", url]
    got = {}
    for lang, t in (("ja", ja), ("en", en)):
        if t:
            got[lang] = tmp / f"{lang}.{TEXT_SUBS[t['codec_name']]}"
            cmd += ["-map", f"0:{t['index']}", "-c", "copy" if t["codec_name"] in ("ass", "ssa", "subrip") else "srt", got[lang]]
    audio = tmp / "audio.wav"
    if not ja:  # Jimaku's file is fitted to this release: Parakeet needs its audio when there's no English track
        cmd += ["-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000", audio]
    step("reading the file once for its subtitles" + ("" if ja else " and audio") + " (a minute or two)")
    jobs._run(cmd)
    english = got["en"].read_text(encoding="utf-8", errors="replace") if "en" in got else ""
    if ja:
        return got["ja"].read_text(encoding="utf-8", errors="replace"), english
    key = os.environ.get("JIMAKU_API_KEY", "")
    if not key:
        raise ValueError("the file has no Japanese subtitles and the PC has no Jimaku key")
    job = {"jimaku": key, "step": ""}
    step("Japanese subtitles from Jimaku")
    cues, _ = jobs._jimaku(job, audio, show, season, number, file, tmp, got.get("en"))
    if not cues:
        raise ValueError("no Japanese subtitles in the file or on Jimaku: pick another file or title")
    jobs._srt(cues, tmp / "ja.srt")
    return (tmp / "ja.srt").read_text(encoding="utf-8"), english


def watch(tv, body: dict) -> dict:
    lang = str(body.get("lang") or "ja")
    # No profile picked (a one-person TV): the first person learning that language
    person = str(body.get("person") or next((p for p, libs in sync.PEOPLE.items() if lang in libs), ""))
    type_, sid = str(body.get("type", "")), str(body.get("id", ""))
    h, file, idx = str(body.get("hash") or "").lower(), str(body.get("file") or ""), str(body.get("idx") or "-1")
    if lang not in sync.PEOPLE.get(person, {}) or type_ not in ("movie", "series") or not stremio.ID.match(sid) \
            or not stremio.HASH.match(h) or not file or not re.fullmatch(r"-?\d+", idx):
        raise ValueError("bad watch request")
    key = f"{person}|{lang}|{h}|{file}"
    with _lock:
        job = _running.get(key)
        if job and job["state"] in ("ready", "failed"):
            del _running[key]  # reported once; asking again starts over (a fresh link: Real-Debrid's expire)
            return job
        if job:
            return job
        job = _running[key] = {"state": "running", "step": "asking Real-Debrid", "error": ""}

    def run():
        token = os.environ.get("RD_API_TOKEN", "")
        try:
            url = _direct(token, h, file, int(idx), lambda s: job.update(step=s))
            eid = _done().get(key)
            if not (eid and (STREAM / f"{eid}.kumapie.json").exists()):
                about = stremio.meta(type_, sid)
                season, number = stremio.numbers(sid)
                title = str(body.get("title") or "")[:200] or (f"{about['name']} {number}" if number else about["name"])
                with tempfile.TemporaryDirectory(dir=cfg.work) as tmp:
                    ja, en = subtitles(url, about["name"], season, number, file, Path(tmp), lambda s: job.update(step=s))
                job.update(step="finding the words (Japanese: about 30 s)")
                eid = tv.stream({"person": person, "lang": lang, "title": title, "show": about["name"],
                                 "subs": ja, "english": en})["id"]
                with _lock:
                    DONE.write_text(json.dumps({**_done(), key: eid}), encoding="utf-8")
            job.update(state="ready", id=eid, url=url, step="")
        except Exception as e:  # noqa: BLE001 - every failure is shown on the phone / TV
            job.update(state="failed", error=str(e)[:300])
    threading.Thread(target=run, daemon=True).start()
    return job


if __name__ == "__main__":
    assert _sub([{"index": 2, "codec_type": "subtitle", "codec_name": "ass", "tags": {"language": "jpn"}}], "ja")["index"] == 2
    assert not _sub([{"index": 2, "codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle", "tags": {"language": "jpn"}}], "ja")
    print("ok")
