"""What a person watched in kumapie, on their AniList list (user, 2026-11-09): Bee's Character Dictionary is made
from that list, so a show watched here gets its characters' names (壬氏様 as one word).

An anime folder matched to AniList (state\\feed\\meta\\<show>.json, "source": "anilist") counts the episodes watched to
the end (every scene seen, on any device, as kumapie's "watched"). The list entry's progress is raised to that, never
lowered; a started show becomes CURRENT, one with every episode COMPLETED. A COMPLETED or REPEATING entry keeps its
status.

config.toml:  [anilist]  user = "<AniList name>"  libraries = ["pedro-ja"]
.env:         ANILIST_TOKEN=<token>   (anilist.co > Settings > Developer: a client with redirect
              https://anilist.co/api/v2/oauth/pin, then
              https://anilist.co/api/v2/oauth/authorize?client_id=<id>&response_type=token; valid a year)

  GET  /api/anilist   what would change (reads the public list, changes nothing)
  POST /api/anilist   change it now
server.py's loop calls changed() every minute; AniList is asked only when what was watched has changed.
"""
import json
import os
import urllib.request

from dojo import cfg

CONF = cfg.raw.get("anilist", {})
API = "https://graphql.anilist.co"
_synced = None  # what was watched at the last sync that went through


def gql(query: str, variables: dict, token: str | None = None) -> dict:
    headers = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "kumapie-server"}  # urllib's own name gets 403
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(API, json.dumps({"query": query, "variables": variables}).encode(), headers)
    with urllib.request.urlopen(req, timeout=20) as r:
        out = json.loads(r.read())
    if out.get("errors"):
        raise ValueError(out["errors"][0].get("message", "AniList error"))
    return out["data"]


def watched(tv) -> dict[int, dict]:
    """AniList id -> {"show", "episodes": watched to the end, "started"} for the configured libraries' anime."""
    tv.synced()
    with tv.tv_db() as db:
        seen = tv.seen_scenes(db)
    out: dict[int, dict] = {}
    for ep in tv.episodes.values():
        if ep["person"] not in CONF.get("libraries", []) or ep["kind"] != "anime":
            continue
        m = tv.show_meta(ep["show"])
        if not m or m.get("source") != "anilist":
            continue
        s = out.setdefault(int(m["id"]), {"show": ep["show"], "episodes": set(), "started": False})
        n = len(seen.get(ep["id"], ()))
        s["started"] = s["started"] or n > 0
        if ep["scenes"] and n >= len(ep["scenes"]):
            s["episodes"].add(ep["episode"] or ep["id"])  # by number: the same episode in two folders counts once
    return {k: v for k, v in out.items() if v["started"]}


def plan(tv) -> list[dict]:
    """The entries to change: [{"id", "show", "progress", "status", "was"}]."""
    mine = watched(tv)
    if not mine or not CONF.get("user"):
        return []
    data = gql("""query ($user: String) { MediaListCollection(userName: $user, type: ANIME) {
        lists { entries { mediaId progress status } } } }""", {"user": CONF["user"]})
    have = {e["mediaId"]: e for li in data["MediaListCollection"]["lists"] for e in li["entries"]}
    totals = gql("query ($ids: [Int]) { Page(perPage: 50) { media(id_in: $ids) { id episodes } } }", {"ids": list(mine)})
    total = {m["id"]: m["episodes"] for m in totals["Page"]["media"]}
    out = []
    for mid, s in sorted(mine.items()):
        was = have.get(mid) or {"progress": 0, "status": None}
        progress = max(was["progress"] or 0, len(s["episodes"]))
        status = was["status"] if was["status"] in ("COMPLETED", "REPEATING") else \
            "COMPLETED" if total.get(mid) and progress >= total[mid] else "CURRENT"
        if progress != (was["progress"] or 0) or status != was["status"]:
            out.append({"id": mid, "show": s["show"], "progress": progress, "status": status,
                        "was": f"{was['status'] or 'not on the list'} {was['progress'] or 0}"})
    return out


def sync(tv, log=print) -> list[dict]:
    """Makes the changes of plan(); needs ANILIST_TOKEN."""
    token = os.environ.get("ANILIST_TOKEN")
    if not token:
        raise ValueError("no ANILIST_TOKEN in .env")
    changes = plan(tv)
    for c in changes:
        gql("""mutation ($id: Int, $progress: Int, $status: MediaListStatus) {
            SaveMediaListEntry(mediaId: $id, progress: $progress, status: $status) { id } }""",
            {"id": c["id"], "progress": c["progress"], "status": c["status"]}, token)
        log(f"anilist: {c['show']} {c['was']} -> {c['status']} {c['progress']}")
    return changes


def changed(tv, log=print) -> None:
    """Every minute: sync when what was watched isn't what it was at the last sync. Nothing without a token."""
    global _synced
    if not os.environ.get("ANILIST_TOKEN") or not CONF.get("user"):
        return
    now = {k: sorted(map(str, v["episodes"])) for k, v in watched(tv).items()}
    if now == _synced:
        return
    try:
        sync(tv, log)
        _synced = now
    except Exception as e:  # AniList down or the token expired: say so, try again next minute
        log(f"anilist: {e}")


def demo():
    """python anilist.py: the rule for one entry, without AniList or the library."""
    class TV:
        episodes = {"a": {"id": "a", "person": "p", "kind": "anime", "show": "s", "episode": 1, "scenes": [1, 2]},
                    "b": {"id": "b", "person": "p", "kind": "anime", "show": "s", "episode": 2, "scenes": [1, 2]},
                    "c": {"id": "c", "person": "other", "kind": "anime", "show": "s", "episode": 3, "scenes": [1]}}
        def synced(self): pass
        def tv_db(self):
            import contextlib
            return contextlib.nullcontext()
        def seen_scenes(self, db): return {"a": {1, 2}, "b": {1}, "c": {1}}
        def show_meta(self, show): return {"source": "anilist", "id": "7"}
    CONF["libraries"] = ["p"]
    assert {k: (sorted(v["episodes"]), v["started"]) for k, v in watched(TV()).items()} == {7: ([1], True)}
    print("ok")


if __name__ == "__main__":
    demo()
