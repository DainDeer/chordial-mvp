"""
Migration: add delivery-eligibility flags.

  - users.is_test              (BOOLEAN, default 0) - synthetic/seed accounts we
                               keep for testing but never send outbound to.
  - platform_identities.is_active (BOOLEAN, default 1) - whether this specific
                               platform link is still deliverable; flipped off
                               when a send hard-fails (discord 404/forbidden).

Base.metadata.create_all() only creates missing *tables*, never new columns on
existing ones, so existing databases need this. Safe to run repeatedly - each
column is only added if it isn't already present.
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
    return {row[1] for row in rows}  # row[1] is the column name


def migrate_delivery_flags():
    engine = create_engine(Config.DATABASE_URL)

    with engine.connect() as conn:
        try:
            user_cols = _columns(conn, "users")
            if "is_test" not in user_cols:
                logger.info("adding users.is_test ...")
                conn.execute(text(
                    "ALTER TABLE users ADD COLUMN is_test BOOLEAN DEFAULT 0"
                ))
            else:
                logger.info("users.is_test already exists, skipping")

            identity_cols = _columns(conn, "platform_identities")
            if "is_active" not in identity_cols:
                logger.info("adding platform_identities.is_active ...")
                conn.execute(text(
                    "ALTER TABLE platform_identities ADD COLUMN is_active BOOLEAN DEFAULT 1"
                ))
            else:
                logger.info("platform_identities.is_active already exists, skipping")

            conn.commit()
            logger.info("migration completed successfully!")

        except Exception as e:
            logger.error(f"migration failed: {e}")
            conn.rollback()
            raise


if __name__ == "__main__":
    migrate_delivery_flags()
