# kumapie server

kumapie's PC side (dojo-feed and its tools); see `README.md` for the map. Pedro studies on his phone and
tablet; this PC is only a server. The app itself is `C:\dojo\kumapie-tv` (own repo); its plan is
`C:\dojo\code\kumapie_plan.md`.

- Paths and people come from `config.toml` (`dojo/config.py`, `cfg`); never hardcode one. Secrets in `.env`.
- Run Python with `.venv\Scripts\python.exe` in UTF-8 mode (`PYTHONUTF8=1`).
- dojo-feed restarts itself within ~5 s after an edit here (`dojo/selfrestart.py`); `Restart-Service dojo-feed`
  otherwise. Logs: `C:\dojo\work\logs\dojo-feed.log`. 127.0.0.1 only, Tailscale Serve (never Funnel) in front.
- `pc_episodes.py`: always name the new `.mp4` files, or it walks the whole library and duplicates episodes.
- `feed.py gloss` (paid) is unused since 2026-10-15: the server serves dictionary meanings only (dicts.py). Don't run it.
- Filenames from titles go through `dojo.config.safe_name()`.
- Commit only your own files (other sessions may be editing): stage explicit paths, never `git commit -a`.
