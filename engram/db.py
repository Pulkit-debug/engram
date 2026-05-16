"""SQLite + sqlite-vec storage layer.

One file at ~/.engram/data/engram.db. WAL mode for concurrent reads. The v1
process.py (lsof/SIGTERM dance) is gone; SQLite gives us concurrent reads
for free.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from importlib import resources
from pathlib import Path
from typing import Iterator, TYPE_CHECKING

if TYPE_CHECKING:
    from engram.config import Config

logger = logging.getLogger(__name__)

_SCHEMA_RESOURCE = "schema.sql"
_VEC_TABLE_FILE_CHUNKS = "vec_file_chunks"
_VEC_TABLE_MEMORY = "vec_memory"


def _load_sqlite_vec(conn: sqlite3.Connection) -> bool:
    """Load the sqlite-vec extension. Returns True if it loaded.

    We never raise here. Embeddings are optional; lexical search is the primary
    signal for DevOps content. If sqlite-vec is missing, vector tables aren't
    created and vector queries return empty.
    """
    try:
        import sqlite_vec
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        return True
    except Exception as exc:
        logger.debug("sqlite-vec not loaded: %s", exc)
        return False


def _read_schema_sql() -> str:
    """Read schema.sql from the package resources."""
    files = resources.files("engram")
    return (files / _SCHEMA_RESOURCE).read_text(encoding="utf-8")


def open_db(config: Config) -> sqlite3.Connection:
    """Open (and lazily initialize) the Engram database."""
    db_path: Path = config.db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path), isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    # Load sqlite-vec before running schema; schema.sql leaves the vec0 virtual
    # tables to be created here (vec0 requires the extension to exist).
    has_vec = _load_sqlite_vec(conn)

    # Bootstrap schema.
    conn.executescript(_read_schema_sql())

    # Create vec0 virtual tables if the extension is available.
    if has_vec:
        try:
            conn.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS {_VEC_TABLE_FILE_CHUNKS} "
                f"USING vec0(chunk_id TEXT PRIMARY KEY, embedding FLOAT[{config.embedding_dim}])"
            )
            conn.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS {_VEC_TABLE_MEMORY} "
                f"USING vec0(memory_id TEXT PRIMARY KEY, embedding FLOAT[{config.embedding_dim}])"
            )
        except sqlite3.OperationalError as exc:
            logger.warning("vec0 virtual tables could not be created: %s", exc)

    return conn


def has_vector_support(conn: sqlite3.Connection) -> bool:
    """Quick check whether the vec0 tables exist on this connection."""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (_VEC_TABLE_FILE_CHUNKS,),
    ).fetchone()
    return row is not None


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block in a single transaction."""
    conn.execute("BEGIN")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def schema_version(conn: sqlite3.Connection) -> int:
    """Return the currently-installed schema version."""
    row = conn.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()
    return int(row["value"]) if row else 0


def integrity_check(conn: sqlite3.Connection) -> bool:
    """SQLite-native integrity check."""
    row = conn.execute("PRAGMA integrity_check").fetchone()
    return row is not None and row[0] == "ok"
