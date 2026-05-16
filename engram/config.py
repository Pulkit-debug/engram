"""Engram configuration.

Single dataclass, env-var overrides, no deep merging of nested YAML. The v1
config layer had a 12-field YAML merger; we replace it with something a new
contributor can fully understand in 60 seconds.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


_USER_CONFIG_DIR = Path.home() / ".engram"
_USER_CONFIG_FILE = _USER_CONFIG_DIR / "config.yaml"


@dataclass
class Config:
    """Runtime configuration. Defaults are tuned for the common solo case."""

    data_dir: Path = _USER_CONFIG_DIR / "data"
    log_dir: Path = _USER_CONFIG_DIR / "logs"

    # Where to look for repos. We default to nothing; the CLI explicitly asks.
    watch_paths: list[Path] = field(default_factory=list)

    # Ignore globs (fnmatch). Dotdirs are skipped wholesale by the crawler.
    ignore_globs: list[str] = field(default_factory=lambda: [
        "node_modules", "__pycache__", "dist", "build", "target",
        ".terraform", "*.lock", "*.min.js", "*.min.css", "*.map",
    ])

    # Hard caps to keep the indexer well-behaved on a $400 laptop.
    max_file_size_kb: int = 500
    chunk_size: int = 1000
    chunk_overlap: int = 200

    # Embeddings are optional. If True, requires `pip install engram[embed]`.
    embeddings_enabled: bool = False
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_dim: int = 384

    # The HMAC key used to sign codified-context emissions. If empty,
    # signing is disabled. Generated on first init.
    signing_key: str = ""

    # Markers used to detect project roots when walking directories.
    project_markers: list[str] = field(default_factory=lambda: [
        ".git", "package.json", "pyproject.toml", "requirements.txt",
        "Dockerfile", "docker-compose.yml", "docker-compose.yaml",
        "Makefile", "go.mod", "Cargo.toml", "Jenkinsfile",
        "Chart.yaml", "kustomization.yaml", "main.tf", "terragrunt.hcl",
    ])

    @property
    def db_path(self) -> Path:
        return self.data_dir / "engram.db"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)


def load_config(path: Path | None = None) -> Config:
    """Load config from ~/.engram/config.yaml (or *path*) over the defaults."""
    cfg = Config()

    src = path or _USER_CONFIG_FILE
    if src.exists():
        with open(src) as f:
            data = yaml.safe_load(f) or {}
        if "data_dir" in data:
            cfg.data_dir = _expand(data["data_dir"])
        if "log_dir" in data:
            cfg.log_dir = _expand(data["log_dir"])
        if "watch_paths" in data and isinstance(data["watch_paths"], list):
            cfg.watch_paths = [_expand(p) for p in data["watch_paths"] if _expand(p).exists()]
        if "ignore_globs" in data and isinstance(data["ignore_globs"], list):
            cfg.ignore_globs = list(data["ignore_globs"])
        for k in ("max_file_size_kb", "chunk_size", "chunk_overlap", "embedding_dim"):
            if k in data and isinstance(data[k], int):
                setattr(cfg, k, data[k])
        if "embeddings_enabled" in data:
            cfg.embeddings_enabled = bool(data["embeddings_enabled"])
        if "embedding_model" in data:
            cfg.embedding_model = str(data["embedding_model"])
        if "signing_key" in data:
            cfg.signing_key = str(data["signing_key"])

    # Env var overrides win over file. Useful for CI and ephemeral testing.
    if os.environ.get("ENGRAM_DATA_DIR"):
        cfg.data_dir = _expand(os.environ["ENGRAM_DATA_DIR"])
    if os.environ.get("ENGRAM_SIGNING_KEY"):
        cfg.signing_key = os.environ["ENGRAM_SIGNING_KEY"]

    cfg.ensure_dirs()
    return cfg


def write_default_user_config() -> Path:
    """Create ~/.engram/config.yaml with sane defaults if it doesn't exist."""
    _USER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if _USER_CONFIG_FILE.exists():
        return _USER_CONFIG_FILE
    _USER_CONFIG_FILE.write_text(
        "# Engram configuration\n"
        "# Run `engram init` to populate watch_paths interactively.\n"
        "watch_paths: []\n"
        "embeddings_enabled: false\n"
    )
    return _USER_CONFIG_FILE


def _expand(p: str | Path) -> Path:
    return Path(os.path.expanduser(os.path.expandvars(str(p)))).resolve()
