"""Project-owned SQLite registry for the PRISM graph-episode mapping.

Phase B persistence: the Graphiti adapter needs a durable
``PRISM episode_key -> real Graphiti-assigned uuid`` mapping so that a
restarted process can (a) short-circuit duplicate writes before they reach
the client and (b) attribute body-less ``search`` results (graphiti-core
0.29.3 returns ``EntityEdge`` objects whose ``episodes`` are uuid references,
not bodies) back to PRISM episodes.  :class:`SQLiteEpisodeRegistry` is
PRISM's own implementation of that durable knowledge.

Design notes
------------
* The registry table lives in the SAME SQLite database file as the
  EvidenceStore text index (``index.db`` under ``PathConfig.data_dir``): one
  PRISM home keeps one SQLite file and the registry inherits the store's
  location rules.  The table is created additively (``CREATE TABLE IF NOT
  EXISTS``), so a database created by an older PRISM version (only
  ``documents``/``document_fts``) migrates in place: existing rows and the
  store's own schema are untouched, and the registry table simply appears on
  first use.
* The registry is per PRISM environment (one data dir), and a PRISM runtime
  writes to exactly one group/database (live Community configs require
  ``group_id == database == "neo4j"``).  Rows still record both labels, the
  table is keyed by ``(episode_key, group_id)`` and every readback
  (key lookup, reverse uuid lookup, listing) is group-scoped, so a backend
  can never see, skip on, or attribute a row recorded under another group -
  the same defensive group-boundary contract as the adapter's search
  filtering.  The composite key is what lets the same deterministic PRISM
  ``episode_key`` exist independently under the offline group and a live
  Graphiti group sharing one database file.
* Databases written before the offline/graphiti groups shared the table
  keyed ``episode_key`` alone (single-column primary key); such tables are
  rebuilt in place on first open - every row is copied unchanged, and the
  composite key then prevents one group's ``put`` from overwriting another
  group's row with the same key.
* ``graphiti_uuid`` is NULLable on purpose: the adapter never fabricates a
  uuid, so an add whose client result carried no usable uuid records the
  episode knowledge without one.  PRISM-key readback and write idempotency
  keep working; the reverse uuid lookup simply cannot match such a row.
* Each row stores the canonical episode body plus the exact structured
  episode fields and audit timestamps (UTC ISO-8601), so a reopened registry
  rebuilds the identical :class:`~prism.graph.GraphEpisode` - name, body,
  evidence and all.
* Never stored: credentials, hosts, ports or absolute paths.  Only PRISM
  keys, the server-assigned uuid, group/database labels, episode content
  and audit timestamps.
* Importing this module never imports graphiti-core or neo4j and touches no
  network; both the live Graphiti path and the default offline path (via the
  persistent :class:`~prism.graph.offline.SQLiteOfflineGraphBackend`) use it
  under their own database/group labels.
"""

from __future__ import annotations

import json
import sqlite3
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from prism.config import PathConfig
from prism.domain import EvidenceLocator
from prism.graph.models import GraphEpisode
from prism.store.service import DB_FILENAME

#: Table holding the durable PRISM episode_key -> Graphiti uuid mapping.
TABLE = "graphiti_episode_registry"

#: Composite primary key: one PRISM key per group, so the offline group and
#: a live Graphiti group can hold the same ``episode_key`` independently.
PRIMARY_KEY = ("episode_key", "group_id")

_TABLE_COLUMNS = """
    episode_key TEXT NOT NULL,
    graphiti_uuid TEXT,
    group_id TEXT NOT NULL,
    database TEXT NOT NULL,
    name TEXT NOT NULL,
    case_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    episode_body TEXT NOT NULL,
    reference_time TEXT NOT NULL,
    valid_at TEXT NOT NULL,
    invalid_at TEXT,
    source_ids TEXT NOT NULL,
    confidence REAL,
    provenance_type TEXT,
    evidence TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (episode_key, group_id)
"""

_DDL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} ({_TABLE_COLUMNS});
CREATE INDEX IF NOT EXISTS {TABLE}_uuid_group_idx
    ON {TABLE} (graphiti_uuid, group_id);
"""

_INSERT_SQL = f"""
INSERT INTO {TABLE} (
    episode_key, graphiti_uuid, group_id, database, name, case_id, kind,
    episode_body, reference_time, valid_at, invalid_at, source_ids,
    confidence, provenance_type, evidence, created_at, updated_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(episode_key, group_id) DO UPDATE SET
    graphiti_uuid = excluded.graphiti_uuid,
    database = excluded.database,
    name = excluded.name,
    case_id = excluded.case_id,
    kind = excluded.kind,
    episode_body = excluded.episode_body,
    reference_time = excluded.reference_time,
    valid_at = excluded.valid_at,
    invalid_at = excluded.invalid_at,
    source_ids = excluded.source_ids,
    confidence = excluded.confidence,
    provenance_type = excluded.provenance_type,
    evidence = excluded.evidence,
    updated_at = excluded.updated_at
"""

_SELECT_COLUMNS = (
    "episode_key, graphiti_uuid, group_id, database, name, case_id, kind,"
    " episode_body, reference_time, valid_at, invalid_at, source_ids,"
    " confidence, provenance_type, evidence"
)

_ALL_COLUMNS = f"{_SELECT_COLUMNS}, created_at, updated_at"


def _iso(value: datetime) -> str:
    return value.isoformat()


def _evidence_payload(
    evidence: tuple[EvidenceLocator, ...],
) -> list[dict[str, Any]]:
    return [
        {
            "source_id": item.source_id,
            "corpus_path": item.corpus_path,
            "paragraph": item.paragraph,
            "page": item.page,
            "quote": item.quote,
        }
        for item in evidence
    ]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SQLiteEpisodeRegistry:
    """Durable, SQLite-backed PRISM episode knowledge keyed by group.

    The registry is bound to one PRISM environment (one ``PathConfig`` /
    data dir) and records the group/database every episode was written
    under; rows are keyed by ``(episode_key, group_id)``, so the same
    deterministic key can exist under the offline group and a live Graphiti
    group independently and every readback is group-scoped.  Connections
    open lazily on first use and the registry must be closed explicitly
    (the composition root closes it on runtime shutdown); operations after
    :meth:`close` fail loudly instead of silently resurrecting state.
    """

    def __init__(self, paths: PathConfig, *, database: str = "") -> None:
        self.paths = paths.resolve()
        self._database = database
        self._connection: sqlite3.Connection | None = None
        self._closed = False
        # Auditable record of rows skipped fail-closed by read paths such as
        # :meth:`list_episodes` (also announced through ``warnings.warn``).
        self._decode_warnings: list[str] = []

    # -- lifecycle ---------------------------------------------------------

    @property
    def db_path(self) -> Path:
        """The SQLite database file (shared with the EvidenceStore index)."""
        return self.paths.data_dir / DB_FILENAME

    @property
    def database(self) -> str:
        """The database label recorded on every row (== group on live configs)."""
        return self._database

    @property
    def closed(self) -> bool:
        return self._closed

    def open(self) -> None:
        """Open the SQLite connection and ensure the table exists (idempotent)."""
        if self._closed:
            raise RuntimeError("registry is closed")
        if self._connection is not None:
            return
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        # Additive migration: databases created by older PRISM versions gain
        # the registry table without any change to their existing rows.
        connection.executescript(_DDL)
        self._migrate_legacy_single_key_table(connection)
        self._connection = connection

    def close(self) -> None:
        """Close the SQLite connection (idempotent)."""
        if self._connection is not None:
            self._connection.close()
            self._connection = None
        self._closed = True

    def __enter__(self) -> "SQLiteEpisodeRegistry":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @staticmethod
    def _migrate_legacy_single_key_table(connection: sqlite3.Connection) -> None:
        """Rebuild a pre-group-scoped table in place (single-key primary key).

        Tables written before the offline and Graphiti groups shared the file
        keyed ``episode_key`` alone, so a ``put`` silently overwrote the other
        group's row carrying the same key.  The rebuild copies every row
        unchanged (a single-key table can never hold composite duplicates,
        including the created_at/updated_at audit timestamps) into the
        composite-keyed shape inside one transaction; SQLite DDL is
        transactional, so a crash leaves either the old or the new table.
        Static SQL only: the table name is a fixed identifier, every value
        travels through bound parameters elsewhere, and ``SELECT *`` is safe
        here because the legacy column order matches this module's DDL (both
        are pinned by the schema tests).
        """
        info = connection.execute(
            "PRAGMA table_info(graphiti_episode_registry)"
        ).fetchall()
        if not info:
            return
        primary_key = tuple(
            row["name"]
            for row in sorted(
                (row for row in info if row["pk"]),
                key=lambda row: row["pk"],
            )
        )
        if primary_key == PRIMARY_KEY:
            return
        connection.executescript(
            "BEGIN;"
            " CREATE TABLE graphiti_episode_registry__migrated ("
            " episode_key TEXT NOT NULL,"
            " graphiti_uuid TEXT,"
            " group_id TEXT NOT NULL,"
            " database TEXT NOT NULL,"
            " name TEXT NOT NULL,"
            " case_id TEXT NOT NULL,"
            " kind TEXT NOT NULL,"
            " episode_body TEXT NOT NULL,"
            " reference_time TEXT NOT NULL,"
            " valid_at TEXT NOT NULL,"
            " invalid_at TEXT,"
            " source_ids TEXT NOT NULL,"
            " confidence REAL,"
            " provenance_type TEXT,"
            " evidence TEXT NOT NULL,"
            " created_at TEXT NOT NULL,"
            " updated_at TEXT NOT NULL,"
            " PRIMARY KEY (episode_key, group_id));"
            " INSERT INTO graphiti_episode_registry__migrated"
            " SELECT * FROM graphiti_episode_registry;"
            " DROP TABLE graphiti_episode_registry;"
            " ALTER TABLE graphiti_episode_registry__migrated"
            " RENAME TO graphiti_episode_registry;"
            " CREATE INDEX IF NOT EXISTS"
            " graphiti_episode_registry_uuid_group_idx"
            " ON graphiti_episode_registry (graphiti_uuid, group_id);"
            " COMMIT;"
        )

    # -- storage -----------------------------------------------------------

    def get(self, episode_key: str, *, group_id: str = "") -> GraphEpisode | None:
        """Return the stored episode for ``episode_key``, or None.

        Group-scoped when ``group_id`` is given: the query filters on
        ``episode_key AND group_id``, so only a row recorded under that
        group can be returned and a foreign group's row can never make a
        caller skip its own write (the offline group and a live Graphiti
        group may legitimately hold the same deterministic key).  The empty
        default keeps the legacy unscoped lookup for existing callers; once
        a key exists under more than one group that lookup is ambiguous, so
        group-aware callers must always pass ``group_id``.
        """
        if not isinstance(episode_key, str) or not episode_key:
            return None
        self._ensure_open()
        if group_id:
            row = self._connection.execute(
                f"SELECT {_SELECT_COLUMNS} FROM {TABLE}"
                " WHERE episode_key = ? AND group_id = ?",
                (episode_key, group_id),
            ).fetchone()
        else:
            row = self._connection.execute(
                f"SELECT {_SELECT_COLUMNS} FROM {TABLE} WHERE episode_key = ?",
                (episode_key,),
            ).fetchone()
        return self._episode_from_row(row) if row is not None else None

    def put(
        self,
        episode: GraphEpisode,
        *,
        group_id: str = "",
        graphiti_uuid: str | None = None,
    ) -> None:
        """Persist ``episode`` under its PRISM episode_key (upsert).

        ``group_id`` is the group/database boundary the episode was written
        under; ``graphiti_uuid`` is the REAL server-assigned uuid the add
        returned, or None when the client result carried no usable uuid
        (nothing is ever fabricated here).
        """
        self._ensure_open()
        now = _now_iso()
        with self._connection:
            self._connection.execute(
                _INSERT_SQL,
                (
                    episode.episode_key,
                    graphiti_uuid,
                    group_id,
                    self._database,
                    episode.name,
                    episode.case_id,
                    episode.kind,
                    episode.episode_body,
                    _iso(episode.reference_time),
                    _iso(episode.valid_at),
                    _iso(episode.invalid_at) if episode.invalid_at else None,
                    json.dumps(list(episode.source_ids), ensure_ascii=False),
                    episode.confidence,
                    episode.provenance_type,
                    json.dumps(_evidence_payload(episode.evidence), ensure_ascii=False),
                    now,
                    now,
                ),
            )

    def get_by_graphiti_uuid(
        self, graphiti_uuid: str, *, group_id: str = ""
    ) -> GraphEpisode | None:
        """Return the episode whose REAL Graphiti uuid is ``graphiti_uuid``.

        Group-scoped: only rows recorded under ``group_id`` can be returned,
        so a backend never attributes a uuid that belongs to another
        group/database.  A row recorded without a uuid (no uuid was ever
        captured) can never be matched here.
        """
        if not graphiti_uuid:
            return None
        self._ensure_open()
        row = self._connection.execute(
            f"SELECT {_SELECT_COLUMNS} FROM {TABLE}"
            " WHERE graphiti_uuid = ? AND group_id = ?",
            (graphiti_uuid, group_id),
        ).fetchone()
        return self._episode_from_row(row) if row is not None else None

    @property
    def decode_warnings(self) -> tuple[str, ...]:
        """Human-readable record of rows skipped fail-closed by read paths."""
        return tuple(self._decode_warnings)

    def list_episodes(self, group_id: str = "") -> tuple[GraphEpisode, ...]:
        """Read-only listing of every episode recorded under ``group_id``.

        Stably ordered by ``(case_id, valid_at, episode_key)`` so a restarted
        process rebuilds timelines in a deterministic order.  Group-scoped:
        rows recorded under any other group (e.g. a live Graphiti group in
        the same database file) are never returned.  Unusable rows
        (tampered or truncated) fail closed: they are skipped — never
        guessed back into results — and each skip is announced through
        ``warnings.warn`` and kept in :attr:`decode_warnings` for audit.
        """
        if not isinstance(group_id, str) or not group_id:
            raise ValueError("group_id is required")
        self._ensure_open()
        rows = self._connection.execute(
            f"SELECT {_SELECT_COLUMNS} FROM {TABLE} WHERE group_id = ?"
            " ORDER BY case_id, valid_at, episode_key",
            (group_id,),
        ).fetchall()
        episodes: list[GraphEpisode] = []
        for row in rows:
            episode = self._episode_from_row(row)
            if episode is None:
                self._record_decode_warning(row["episode_key"], group_id)
                continue
            episodes.append(episode)
        return tuple(episodes)

    def _record_decode_warning(self, episode_key: object, group_id: str) -> None:
        message = (
            f"graphiti_episode_registry row {episode_key!r} in group"
            f" {group_id!r} is unreadable and was skipped (fail-closed);"
            " no episode was reconstructed from it"
        )
        self._decode_warnings.append(message)
        warnings.warn(message, RuntimeWarning, stacklevel=3)

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError(
                "registry is closed; a registry closed by the runtime cannot "
                "be used again"
            )
        self.open()

    @staticmethod
    def _episode_from_row(row: sqlite3.Row) -> GraphEpisode | None:
        """Rebuild the exact stored episode from one row, or None.

        Any unusable payload (tampered or truncated row) yields None rather
        than a guessed episode; ``GraphEpisode``'s own validation backs this
        up (intervals, aware datetimes, evidence/source consistency).
        """
        try:
            evidence = tuple(
                EvidenceLocator(
                    source_id=str(item["source_id"]),
                    corpus_path=str(item["corpus_path"]),
                    paragraph=item.get("paragraph"),
                    page=item.get("page"),
                    quote=item.get("quote"),
                )
                for item in json.loads(row["evidence"])
            )
            invalid_at = row["invalid_at"]
            try:
                body_payload = json.loads(row["episode_body"])
            except (TypeError, ValueError):
                body_payload = {}
            evidence_role = (
                body_payload.get("evidence_role")
                if isinstance(body_payload, dict)
                else None
            )
            cited_source_ref = (
                body_payload.get("cited_source_ref")
                if isinstance(body_payload, dict)
                else None
            )
            return GraphEpisode(
                episode_key=row["episode_key"],
                name=row["name"],
                case_id=row["case_id"],
                kind=row["kind"],
                episode_body=row["episode_body"],
                reference_time=datetime.fromisoformat(row["reference_time"]),
                valid_at=datetime.fromisoformat(row["valid_at"]),
                invalid_at=(
                    datetime.fromisoformat(invalid_at) if invalid_at else None
                ),
                source_ids=tuple(json.loads(row["source_ids"])),
                confidence=row["confidence"],
                provenance_type=row["provenance_type"],
                evidence=evidence,
                evidence_role=evidence_role,
                cited_source_ref=cited_source_ref,
            )
        except (AttributeError, KeyError, TypeError, ValueError):
            return None


__all__ = ["SQLiteEpisodeRegistry", "TABLE"]
