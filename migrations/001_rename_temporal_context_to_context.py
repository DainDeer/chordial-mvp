"""
Migration script to rename temporal_context to context in conversation_history table
Run this once to update existing databases
"""
from sqlalchemy import create_engine, text
import logging

import sys
import os

# this gets the directory of the current script (e.g., /path/to/your/repo/migrations)
script_dir = os.path.dirname(os.path.abspath(__file__))
# this gets the parent directory (the project root, e.g., /path/to/your/repo)
project_root = os.path.dirname(script_dir)
# this adds the project root to the list of places python looks for imports
sys.path.insert(0, project_root)

from config import Config

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def migrate_context_field():
    """rename temporal_context to context in conversation_history"""
    engine = create_engine(Config.DATABASE_URL)
    
    with engine.connect() as conn:
        try:
            # check if migration is needed
            result = conn.execute(text(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='conversation_history'"
            )).fetchone()
            
            if result and 'temporal_context' in result[0]:
                logger.info("migrating temporal_context to context...")
                
                # sqlite doesn't support ALTER COLUMN, so we need to recreate
                conn.execute(text("""
                    CREATE TABLE conversation_history_new (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id VARCHAR,
                        platform VARCHAR,
                        role VARCHAR,
                        content VARCHAR,
                        context JSON,
                        created_at DATETIME,
                        FOREIGN KEY(user_id) REFERENCES users (id)
                    )
                """))
                
                # copy data
                conn.execute(text("""
                    INSERT INTO conversation_history_new 
                    (id, user_id, platform, role, content, context, created_at)
                    SELECT id, user_id, platform, role, content, temporal_context, created_at
                    FROM conversation_history
                """))
                
                # drop old table and rename new
                conn.execute(text("DROP TABLE conversation_history"))
                conn.execute(text("ALTER TABLE conversation_history_new RENAME TO conversation_history"))
                
                conn.commit()
                logger.info("migration completed successfully!")
            else:
                logger.info("no migration needed - context field already exists")
                
        except Exception as e:
            logger.error(f"migration failed: {e}")
            conn.rollback()
            raise

if __name__ == "__main__":
    migrate_context_field()