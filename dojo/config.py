"""Loads config.toml and .env (both in this repo) and exposes every path the scripts need.

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # the repo
    from dojo import cfg

    cfg.library("alice") / "shows"      # C:\\dojo\\library\\alice\\shows
    cfg.python                             # .venv\\Scripts\\python.exe
"""
import os
import re
import shutil
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

CODE = Path(__file__).resolve().parents[1]


def _expand(p: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(p)))


def _load_dotenv(path: Path) -> None:
    """KEY=value lines (optionally `export KEY=value`, quoted or not); the real environment wins."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*['\"]?(.*?)['\"]?\s*$", line)
        if m and not line.lstrip().startswith("#"):
            os.environ.setdefault(m.group(1), m.group(2))


@dataclass(frozen=True)
class Person:
    key: str
    name: str
    transcript: str
    translation: str


class Config:
    def __init__(self) -> None:
        self.raw = tomllib.loads((CODE / "config.toml").read_text(encoding="utf-8"))
        _load_dotenv(CODE / ".env")
        self.root = _expand(os.environ.get("DOJO_ROOT", str(CODE.parent))).resolve()
        self.code = CODE
        p = self.raw["paths"]
        self.library_root = self.root / p["library"]
        self.state = self.root / p["state"]
        self.work = self.root / p["work"]
        self.archive = self.root / p["archive"]
        self.pipeline = CODE / "pipeline"
        self.kumapie_app = self.root / self.raw["kumapie"]["app"]
        self.people = {key: Person(key=key, name=v["name"], transcript=v["transcript"], translation=v["translation"])
                       for key, v in self.raw["people"].items()}
        self.services = self.raw.get("services", {})

    def person(self, key: str) -> Person:
        return self.people[key]

    def library(self, key: str) -> Path:
        return self.library_root / key

    @property
    def python(self) -> Path:
        p = CODE / ".venv" / "Scripts" / "python.exe"
        return p if p.exists() else Path(sys.executable)

    @staticmethod
    def which(name: str) -> str:
        """Full path of a CLI on PATH (resolves Windows .exe/.cmd shims)."""
        found = shutil.which(name)
        if not found:
            sys.exit(f"'{name}' is not installed or not on PATH")
        return found


def safe_name(name: str) -> str:
    """A file/folder name that is valid on Windows (no :*?"<>|/\\, no trailing dots/spaces)."""
    return re.sub(r'[/\\:*?"<>|]', "", name).strip().rstrip(". ")


cfg = Config()
