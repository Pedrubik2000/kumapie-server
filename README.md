# kumapie server

The PC side of kumapie (the phone/tablet/TV app, repo `Pedrubik2000/kumapie-tv`): the **dojo-feed** service and the
tools that fill it. Runs on the Windows PC in `C:\dojo\kumapie-server`; media in `C:\dojo\library`, data in
`C:\dojo\state` (paths in `config.toml`, relative to `C:\dojo`).

| path | what |
|---|---|
| `services\feed\server.py` | dojo-feed, 127.0.0.1:8769 (Tailscale Serve HTTPS 8445): the German feed page and `/api/feed` |
| `services\feed\tv.py` | `/api/tv/...`: shows, episodes and scenes the phones list and stream (`?person=`) |
| `services\feed\sync.py` | episodes made on a device, uploaded to `library\<key>\` |
| `services\feed\known.py` | the words each person's kumapie shows as known |
| `services\feed\dicts.py` | Yomitan dictionaries for the TV's lookups |
| `services\feed\jobs.py` | episodes made on the PC for kumapie: AniWorld, YouTube, TikTok, Real-Debrid, uploaded files |
| `tools\feed\feed.py` | German feed clips, scenes and glosses (`tools\feed\README.md`) |
| `tools\kumapie\pc_episodes.py` | library videos -> kumapie episodes |
| `tools\kumapie\ja_words.py`, `jiten_split.ps1`, `jiten_lexicon.py` | Japanese words with the phones' own splitter |
| `pipeline\srt_spaced.py`, `parakeet.py` | transcript JSON -> `.srt`; free CPU transcription |
| `dojo\` | `config.toml` + `.env` loader, self-restart on code changes |

It imports some of the app's Python modules (`meta.py`, `newepisode.py`, `morphemizer.py`) from the app checkout
named in `config.toml` `[kumapie] app`.

## Setup

```powershell
uv sync                                   # .venv: spaCy + German/English models, edge-tts, anthropic, sudachi
copy config.example.toml config.toml     # then set your people and libraries
copy .env.example .env                    # only feed.py gloss needs a key
```

The Windows service (NSSM, LocalSystem) is installed by the dojo repo's `windows\services.ps1`; it restarts itself
within ~5 s when code here changes.

## After new videos

```powershell
.venv\Scripts\python.exe tools\kumapie\pc_episodes.py --for <key> <new .mp4 files>   # always name the new files
.venv\Scripts\python.exe tools\feed\feed.py build                                    # new German video
.venv\Scripts\python.exe tools\feed\feed.py gloss --estimate                         # paid: ask first
```
