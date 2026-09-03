import os
from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session

from database.models import Base
from utils.config import config


DATABASE_URL = config.database_config.url
connect_args = config.database_config.connect_args
echo_queries = config.database_config.echo_queries

engine = create_engine(
    DATABASE_URL,
    connect_args=connect_args,
    echo=echo_queries
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def init_db() -> None:
    """
    Initializes the database by creating all defined tables.
    
    If using SQLite, this will also ensure that the directory containing 
    the database file exists before attempting to create the tables.
    """
    if DATABASE_URL.startswith("sqlite"):
        db_path = DATABASE_URL.replace("sqlite:///", "")
        
        if db_path and db_path != ":memory:":
            db_dir = os.path.dirname(db_path)
            if db_dir and not os.path.exists(db_dir):
                os.makedirs(db_dir, exist_ok=True)

    Base.metadata.create_all(bind=engine)


def get_db() -> Generator[Session, None, None]:
    """
    Provides a transactional scope around a series of database operations.
    
    Yields:
        Session: A SQLAlchemy database session.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()