from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from contextlib import contextmanager
import logging

from .models import Base
from config import Config

logger = logging.getLogger(__name__)

# create engine
engine = create_engine(
    Config.DATABASE_URL,
    echo=False,  # set to true for sql query logging
    connect_args={"check_same_thread": False} if "sqlite" in Config.DATABASE_URL else {}
)

# create session factory
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def init_db():
    """initialize database tables"""
    logger.info("creating database tables...")
    Base.metadata.create_all(bind=engine)
    logger.info("database tables created!")

@contextmanager
def get_db() -> Session:
    """provide a transactional scope for database operations"""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

# convenience function for async context
async def get_db_session() -> Session:
    """get a database session for async operations"""
    return SessionLocal()