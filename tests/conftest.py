"""
Pytest configuration and shared fixtures for the E-Commerce AI Assistant.

This file sets up the test environment, including:
- An in-memory SQLite database to ensure tests run fast and isolated.
- Mocking of the application's `SessionLocal` to point to the test database.
- Flask test clients (both unauthenticated and authenticated).
"""

import pytest
from sqlalchemy import create_engine
from werkzeug.security import generate_password_hash

from app import create_app
from database.db_setup import SessionLocal
from database.models import Base, User, UserRole


@pytest.fixture(scope="session")
def test_engine():
    """Creates a single in-memory SQLite engine for the entire test session."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    SessionLocal.configure(bind=engine)
    yield engine
    Base.metadata.drop_all(bind=engine)


@pytest.fixture(scope="function")
def db_session(test_engine, monkeypatch):
    """
    Provides a transactional database session for a single test.
    Patches the app's `SessionLocal` so all routes use this test database.
    Cleans up all table data after the test completes.
    """
    session = SessionLocal()
    yield session
    
    session.rollback()
    
    for table in reversed(Base.metadata.sorted_tables):
        session.execute(table.delete())
    session.commit()
    session.close()

@pytest.fixture(scope="function")
def app_instance():
    """Yields the Flask application configured for testing."""
    flask_app = create_app()
    flask_app.config.update({
        "TESTING": True,
        "WTF_CSRF_ENABLED": False,
        "SERVER_NAME": "localhost.localdomain",
        "RATELIMIT_ENABLED": False
    })
    
    with flask_app.app_context():
        yield flask_app


@pytest.fixture(scope="function")
def client(app_instance, db_session):
    """
    An unauthenticated test client.
    Requires `db_session` to ensure the DB monkeypatch is active during requests.
    """
    with app_instance.test_client() as testing_client:
        yield testing_client


@pytest.fixture(scope="function")
def admin_client(client, db_session):
    """
    A test client pre-authenticated as a user with Admin privileges.
    """
    admin = User(
        email="admin_test@example.com",
        name="Admin Test",
        password_hash="mock_hash_for_testing",
        role=UserRole.ADMIN,
        is_active=True
    )
    db_session.add(admin)
    db_session.commit()
    
    with client.session_transaction() as sess:
        sess["user_id"] = admin.id
        
    yield client

@pytest.fixture(scope="function")
def admin_user(db_session):
    """Provides an active admin user with a known password (does not log them in)."""
    user = User(
        email="admin_login@test.com",
        name="Test Admin",
        password_hash=generate_password_hash("adminpass123"),
        role=UserRole.ADMIN,
        is_active=True
    )
    db_session.add(user)
    db_session.commit()
    return user

@pytest.fixture(scope="function")
def customer_user(db_session):
    """Provides a standard active customer user with a known password."""
    user = User(
        email="customer@test.com",
        name="Test Customer",
        password_hash=generate_password_hash("password123"),
        role=UserRole.CUSTOMER,
        is_active=True
    )
    db_session.add(user)
    db_session.commit()
    return user