"""
Idempotent schema upgrades. db.create_all() creates missing TABLES but never adds a
column to an existing one, and there is no migration tool yet, so new columns on
existing tables are added here. Every statement is safe to run on every start.
(A proper reversible migration arrives with the Project/Session/File restructure.)
"""
from sqlalchemy import text

ADD_COLUMNS = [
    ("resources", "captured_at_precision", "VARCHAR DEFAULT 'unknown'"),
    ("resources", "suggested_captured_at", "TIMESTAMP"),
    ("resources", "filename_info", "JSON"),
    ("resources", "session_id", "VARCHAR REFERENCES sessions(id)"),
    ("resources", "role", "VARCHAR NOT NULL DEFAULT 'original'"),
    ("resources", "derived_from_id", "VARCHAR REFERENCES resources(id)"),
    ("resources", "track_label", "VARCHAR"),
    ("resources", "notes", "TEXT"),
    ("resources", "size_bytes", "BIGINT"),
    ("resources", "waveform_at", "TIMESTAMP"),
    ("resources", "waveform_error", "TEXT"),
    ("resources", "preview_at", "TIMESTAMP"),
    ("resources", "preview_error", "TEXT"),
    ("locations", "place_name", "VARCHAR"),
    ("locations", "place_source", "VARCHAR"),
    ("locations", "place_info", "JSON"),
    ("locations", "place_checked_at", "TIMESTAMP"),
    ("projects", "placement", "VARCHAR NOT NULL DEFAULT 'nas'"),
    ("projects", "home", "VARCHAR NOT NULL DEFAULT 'nas'"),
    ("projects", "created_at", "TIMESTAMP DEFAULT NOW()"),
    # Split-file / multitrack grouping (PLAN 22)
    ("resources", "group_id", "VARCHAR"),
    ("resources", "group_type", "VARCHAR"),
    ("resources", "group_status", "VARCHAR"),
    ("resources", "group_reason", "TEXT"),
    ("resources", "group_error", "TEXT"),
    ("resources", "joined_into_id", "VARCHAR REFERENCES resources(id)"),
    ("resources", "joined_from_ids", "JSON"),
    ("recorder_profiles", "split_seconds", "INTEGER"),
]


def ensure_schema(db):
    with db.engine.begin() as conn:
        for table, column, ddl in ADD_COLUMNS:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {ddl}"))
        # Rows that predate the precision column and already carry a date were entered or
        # read as exact times. (Invariant kept everywhere else: a date implies exact or
        # approximate, never unknown, so this cannot touch a deliberate choice.)
        conn.execute(text(
            "UPDATE resources SET captured_at_precision = 'exact' "
            "WHERE captured_at IS NOT NULL AND (captured_at_precision IS NULL OR captured_at_precision = 'unknown')"
        ))
        conn.execute(text(
            "UPDATE resources SET captured_at_precision = 'unknown' "
            "WHERE captured_at IS NULL AND captured_at_precision IS NULL"
        ))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_resources_group_id ON resources (group_id)"))
        # The Insta360 mic profile was seeded before split_seconds existed (jobs/profiles.py only
        # inserts a missing row by name, never updates one that's already there), so a live install
        # needs this one-time backfill to mark it as the 30-minute splitter (PLAN 18.5f).
        conn.execute(text(
            "UPDATE recorder_profiles SET split_seconds = 1800 "
            "WHERE name = 'Insta360 mic' AND split_seconds IS NULL"
        ))
