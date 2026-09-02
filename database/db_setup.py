import os
from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session

from database.models import Base


# Determine the database URL, defaulting to a local SQLite database in the 'instance' folder.
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///instance/ecommerce.db")

# SQLite requires specific threading configurations when used with web frameworks like Flask.
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(
    DATABASE_URL,
    connect_args=connect_args,
    echo=os.getenv("SQLALCHEMY_ECHO", "False").lower() in ("true", "1", "t")
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