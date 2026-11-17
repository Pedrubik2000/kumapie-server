"""Watched time ranges of an episode: [[start_ms, end_ms], ...], sorted, not overlapping (kumapie_plan.md "no more scenes").

They replace scene ids as what "seen" means: a subtitle fix can't lose them. kumapie has the same three functions in
core/.../data/Ranges.kt; keep the two constants the same there.

Self-check: python ranges.py
"""
from __future__ import annotations

JOIN_MS = 1_000    # ranges closer than this are one (a seek's rounding, a player tick)
SAME_S = 0.05      # cues that start within this of each other are one line (seconds); the app: Api.kt SAME
SLACK = 0.03       # an episode is watched when at most this share of its spoken time is missing


def union(spans) -> list[list[int]]:
    out: list[list[int]] = []
    for a, b in sorted((int(a), int(b)) for a, b in spans if b > a):
        if out and a <= out[-1][1] + JOIN_MS:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def missing(need, have) -> int:
    """Milliseconds of [need] that [have] doesn't cover (both unions)."""
    total = 0
    for a, b in need:
        at = a
        for c, d in have:
            if d <= at:
                continue
            if c >= b:
                break
            total += max(0, c - at)
            at = max(at, d)
        total += max(0, b - at)
    return total


def spoken(cues) -> list[list[int]]:
    """What must be watched: the time of the lines that are said. [cues]: their (start_s, end_s), no lyrics.
    Silence, the opening and the ending have no such lines, so skipping them keeps an episode watched."""
    return union([a * 1000, b * 1000] for a, b in cues)


def together(cues) -> list:
    """Cues shown together are one line: [(place of its first cue, its cues)], cues in order. Together = the same
    start (SAME_S): downloaded subtitles cut one subtitle into pieces (a name, a thought in 《》, each row of it)."""
    out: list = []
    for i, c in enumerate(cues):
        if out and abs(c["start"] - out[-1][1][0]["start"]) < SAME_S:
            out[-1][1].append(c)
        else:
            out.append((i, [c]))
    return out


def watched(need, have) -> bool:
    size = sum(b - a for a, b in need)
    return size > 0 and missing(need, have) <= size * SLACK


if __name__ == "__main__":
    assert union([[5000, 9000], [0, 4500], [20000, 30000], [3, 3]]) == [[0, 9000], [20000, 30000]]
    assert missing([[0, 10000]], [[2000, 4000], [9000, 20000]]) == 2000 + 5000
    assert missing([[0, 10000]], []) == 10000
    need = spoken([(90, 95), (100, 130), (135, 600), (700, 720)])  # 0-90 s opening, silences between the lines
    assert need == [[90000, 95000], [100000, 130000], [135000, 600000], [700000, 720000]], need
    assert watched(need, [[88000, 601000], [699000, 730000]])
    assert watched(need, [[90000, 96000], [99000, 131000], [134000, 600000], [700000, 720000]])  # silences skipped
    assert watched(need, [[95000, 600000], [700000, 720000]])       # 5 s missing of 520 s
    assert not watched(need, [[90000, 600000]])                      # the last line never played
    assert not watched([], [[0, 1000]])
    c = lambda a, b: {"start": a, "end": b}
    assert together([c(1, 2), c(1, 2.5), c(2.6, 3), c(2.62, 3), c(9, 10)]) == [(0, [c(1, 2), c(1, 2.5)]), (2, [c(2.6, 3), c(2.62, 3)]), (4, [c(9, 10)])]
    print("ok")
