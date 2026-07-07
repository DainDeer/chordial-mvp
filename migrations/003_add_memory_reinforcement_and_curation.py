"""
Migration: add reinforcement + curation columns to the memories table.

  - reinforced_count    (INTEGER,  default 0)  - times a duplicate save bumped
                        this row instead of inserting a new one.
  - last_reinforced_at  (DATETIME, NULL)       - when it was last reinforced.
  - curated_at          (DATETIME, NULL)       - NULL = pending curator review.
  - merged_into         (INTEGER,  NULL)       - canonical row id when this one
                        was absorbed by a curator merge.

Additive + idempotent (each column only added if missing), same pattern as 002.
create_all() won't retrofit columns onto existing databases, so existing DBs
need this.
"""
from sqlalchemy import create_engine, text
import logging

import sys
import os

script_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(script_dir)
sys.path.insert(0, project_root)

from config import Config

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _columns(conn, table: str) -> set:
    rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    return {row[1] for row in rows}


# column name -> DDL type/default to add
_NEW_COLUMNS = {
    "reinforced_count": "INTEGER DEFAULT 0",
    "last_reinforced_at": "DATETIME",
    "curated_at": "DATETIME",
    "merged_into": "INTEGER",
}


def migrate_memory_curation():
    engine = create_engine(Config.DATABASE_URL)

    with engine.connect() as conn:
        try:
            existing = _columns(conn, "memories")
            for name, ddl in _NEW_COLUMNS.items():
                if name not in existing:
                    logger.info("adding memories.%s ...", name)
                    conn.execute(text(f"ALTER TABLE memories ADD COLUMN {name} {ddl}"))
                else:
                    logger.info("memories.%s already exists, skipping", name)

            conn.commit()
            logger.info("migration completed successfully!")

        except Exception as e:
            logger.error(f"migration failed: {e}")
            conn.rollback()
            raise


if __name__ == "__main__":
    migrate_memory_curation()
