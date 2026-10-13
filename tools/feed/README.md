# German feed

A TikTok-style vertical feed of 5-15 s clips from Pedro's German library (`library\pedro-de`), only
the ones that are i+0 or i+1 for him today, opened on the phone. Tap a word for its meaning (and its
audio), tap the box at the bottom for the English line. Plan and decisions: `plans/history/german_feed_plan.md` in the dojo repo.

Three parts, all settings in `config.toml` `[feed]`:

| part | what | runs with |
|---|---|---|
| `tools\feed\feed.py build` | groups subtitle cues into clips, parses them with `morphemizer.py` (spaCy), cuts them with ffmpeg into `state\feed\clips\`, indexes them in `state\feed\feed.db` | `.venv` |
| `tools\feed\feed.py gloss` | one Claude request per clip: every word's meaning in that clip (`prompts\gloss_v1.md`, the v20_en style) | `.venv`; key in `.env` (`llm.py`) |
| `services\feed\server.py` (dojo-feed, 127.0.0.1:8769) | the page and `/api/feed`; known words from kumapie (`known.py`) | `.venv`, Windows service |

## After new German videos

```
.venv\Scripts\python.exe tools\feed\feed.py build
.venv\Scripts\python.exe tools\feed\feed.py gloss --estimate
.venv\Scripts\python.exe tools\feed\feed.py gloss
```

`build` is resumable and skips finished clips; it also removes clips whose video or subtitles are gone
or re-timed (a clip's id is a hash of video, start and end). `--only s1_05` limits it to some videos.
`gloss` only sends clips with words that have no meaning yet and prints the cost first; it is paid,
about $0.003 per clip with Haiku 4.5. `gloss --limit 20` glosses a random sample and prints it, to
check a prompt change. `feed.py show [ID]` prints a clip as the service sees it.

Without the API: `feed.py export --pool <dir>` writes today's unglossed feed clips as JSON batches;
write `{clip id: {word: meaning}}` answers (e.g. in a Claude session, following `prompts\gloss_v1.md`)
and load them with `feed.py import <files>`. Done this way on 2026-10-03, when the OpenRouter key had expired.

`build` skips clips that are not German speech: non-Latin script (the Japanese opening song) or a
German line the same as its English one (English speech kept as is in the YouTube video).

The service picks up a new `feed.db` within a minute; no restart needed.

## How clips are made

- Cues closer than `max_gap` (1.5 s) form a run; runs longer than `max_secs` (15 s) are split at their
  biggest pauses (a pause after `.!?` counts more). Clips under `min_words` (4) words are dropped.
- `pad` (0.3 s) is added on both sides, never into a neighbouring clip's cue. The `.srt` cues already
  start 0.5 s early (srt_spaced LEAD_IN).
- English: the `.en.srt` cues whose middle falls inside the clip, without the `(German)` hints.
- Video: H.264 480p crf 26, AAC 96k, `+faststart`, so every clip starts on a keyframe and streams at once.

## Level and known words

A clip's level is the number of its distinct morphs (inflections, as the Anki `_card-status` tags count
them) that aren't known. Known = what kumapie on Pedro's phone draws white or green (its own Mined card
studied, or marked known), sent to `POST /api/tv/known_words` and kept in `state\feed\known\pedro-de.json`
(`services\feed\known.py`), plus the words marked known on the TV. No file yet = nothing known (since
2026-10-11; the old `morphs known` run is gone). The feed serves level 0 and 1, `i1_share` of them i+1.

Known-ness is never stored in `feed.db`: it changes daily, so the level is computed in memory.

## Scenes for the TV app (kumapie-tv)

`feed.py scenes` splits every whole video into scenes for the TV app (plan: `kumapie_plan.md`). A scene
is the same exchange as a clip (same `exchanges()`, same id), but every one is kept: short ones too, and
lines that are not German speech (songs, English) with `german = 0`. Nothing is cut; the TV streams the
whole episode and seeks. Tables in `feed.db`: `episodes` (show, episode, length), `scenes` (times are
episode times, English cues as JSON), `scene_cues` (German lines with their tokens). A feed clip is a scene,
so glosses written for one serve both; `export --scenes` writes the scenes' words still without a gloss.
Run it after `build` when videos change; `--only s1_01 --print` shows how an episode was split.
The service serves them to the TV under `/api/tv/...` (`services\feed\tv.py`, its docstring lists the
routes) and keeps the TV's progress (resume point, scenes seen, minutes per day) in `state\feed\tv.db`.

**Meanings for the TV: one per word** (decided 2026-10-04, instead of one per word per scene). The
`word_glosses` table holds a word's own meaning; a scene's meaning (in `glosses`, by scene id) is used
where the word means something else there. Both are written in Claude sessions, not through the API:

```
.venv\Scripts\python.exe tools\feed\feed.py export-words C:\dojo\work\words\ep02 --only s1_02
(write {"words": {word: meaning}, "scenes": {scene id: {word: meaning}}} per batch, gloss_v1.md style)
.venv\Scripts\python.exe tools\feed\feed.py import-words C:\dojo\work\words\ep02\answer_*.json
```

`export-words` lists only words with no meaning anywhere yet (the feed's clip meanings count), each with
its dictionary form and up to three of its lines. Episode 1 was done this way (397 words + 23 scene
meanings; helper script kept in `C:\dojo\work\words\ep01\answer.py`).

`read_srt` cuts a cue over 8 s that is longer than 0.3 s per character (a transcription glitch held the
last word over the 90 s ending song in two episodes).

## Phone

`tailscale serve --bg --https=8445 http://127.0.0.1:8769` (tailnet only, never Funnel), then open
`https://<pc>.<tailnet>.ts.net:8445` in Chrome and "Add to Home screen" for full screen.
