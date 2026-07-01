#!/usr/bin/env python3
"""Standalone evidence capture for bdp2610 schema optimizations.

Applies the full Alembic chain via ``get_or_create_engine`` (verification
plan step 2) and writes an inspector transcript confirming hot-path indexes,
the comments FK, and absence of redundant indexes.

Usage::

    python scripts/dev/verify_schema_opt_evidence.py \\
        --output /tmp/grok-goal-.../implementer/inspector-transcript.log
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

import sqlalchemy as sa

from omnigent.db.utils import clear_engine_cache, get_or_create_engine

_REQUIRED_CONV_INDEXES = frozenset(
    {
        "ix_conversations_runner_id",
        "ix_conversations_agent_id",
        "ix_conversations_active_sessions",
    }
)
_REQUIRED_COMMENT_FKS = frozenset({"fk_comments_conversation_id"})
_REDUNDANT_INDEXES = {
    ("workforce_instructions", "ix_workforce_instructions_scope"),
    ("goal_dependencies", "ix_goal_dependencies_goal"),
}


def _index_names(inspector: sa.Inspector, table: str) -> set[str]:
    return {idx["name"] for idx in inspector.get_indexes(table)}


def _fk_names(inspector: sa.Inspector, table: str) -> set[str]:
    return {fk["name"] for fk in inspector.get_foreign_keys(table)}


def build_schema_opt_transcript(engine: sa.Engine) -> str:
    """Return a multi-line inspector transcript for a head-upgraded database."""
    inspector = sa.inspect(engine)
    conv_indexes = sorted(_index_names(inspector, "conversations"))
    comment_fks = sorted(_fk_names(inspector, "comments"))
    workforce_indexes = sorted(_index_names(inspector, "workforce_instructions"))
    goal_dep_indexes = sorted(_index_names(inspector, "goal_dependencies"))

    missing_conv = sorted(_REQUIRED_CONV_INDEXES - set(conv_indexes))
    missing_fks = sorted(_REQUIRED_COMMENT_FKS - set(comment_fks))
    if missing_conv:
        raise SystemExit(f"missing conversations indexes: {missing_conv}")
    if missing_fks:
        raise SystemExit(f"missing comments foreign keys: {missing_fks}")

    for table, redundant in _REDUNDANT_INDEXES:
        if redundant in _index_names(inspector, table):
            raise SystemExit(f"redundant index still present: {table}.{redundant}")

    lines = [
        "schema_opt inspector transcript",
        f"engine={engine.url}",
        f"conversations.indexes={conv_indexes}",
        f"comments.foreign_keys={comment_fks}",
        f"workforce_instructions.indexes={workforce_indexes}",
        f"goal_dependencies.indexes={goal_dep_indexes}",
        "redundant_indexes_absent=True",
    ]
    return "\n".join(lines)


def _write_pg_skip(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "postgresql unavailable: no POSTGRES_URL or DATABASE_URL with postgresql dialect\n",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Write the inspector transcript to this file (and stdout).",
    )
    parser.add_argument(
        "--pg-skip-output",
        type=Path,
        default=None,
        help="When Postgres is unavailable, write skip notice here.",
    )
    parser.add_argument(
        "--postgres-url",
        type=str,
        default=None,
        help="Optional Postgres URL to probe GIN FTS index (dialect-guarded).",
    )
    args = parser.parse_args(argv)

    with tempfile.TemporaryDirectory(prefix="schema-opt-evidence-") as tmp:
        db_path = Path(tmp) / "schema_opt_evidence.db"
        uri = f"sqlite:///{db_path}"
        engine = get_or_create_engine(uri)
        try:
            transcript = build_schema_opt_transcript(engine)
        finally:
            clear_engine_cache()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(transcript + "\n", encoding="utf-8")
    print(transcript)

    pg_url = args.postgres_url
    if pg_url and pg_url.startswith("postgresql"):
        pg_engine = sa.create_engine(pg_url)
        try:
            inspector = sa.inspect(pg_engine)
            fts_indexes = _index_names(inspector, "conversation_items")
            if "ix_conversation_items_search_fts" not in fts_indexes:
                raise SystemExit(
                    "missing Postgres GIN index ix_conversation_items_search_fts "
                    f"on conversation_items; found {sorted(fts_indexes)}"
                )
            pg_note = (
                "postgresql fts index present: ix_conversation_items_search_fts\n"
            )
            print(pg_note, end="")
            args.output.write_text(
                args.output.read_text(encoding="utf-8") + pg_note,
                encoding="utf-8",
            )
        finally:
            pg_engine.dispose()
    elif args.pg_skip_output is not None:
        _write_pg_skip(args.pg_skip_output)
        print(f"postgres probe skipped; wrote {args.pg_skip_output}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())