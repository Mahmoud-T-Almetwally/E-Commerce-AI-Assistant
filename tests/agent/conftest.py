import pytest
from unittest.mock import patch
from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session

from database.models import Base, User, Product, Tag, UserRole


@pytest.fixture(scope="session")
def engine():
    """Create a single in-memory SQLite engine for the test session."""
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(bind=engine)
    return engine


@pytest.fixture(scope="function")
def db_session():
    """
    Creates a fresh in-memory database for EVERY test and re-binds the 
    global SessionLocal to use it. This guarantees 100% isolation and 
    bypasses all import/patching namespace issues.
    """
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(bind=engine)
    
    from database.db_setup import SessionLocal
    
    SessionLocal.configure(bind=engine)
    
    session = SessionLocal()
    yield session
    
    session.close()
    engine.dispose()

@pytest.fixture(scope="function")
def seed_data(db_session):
    """Provides a baseline set of records for tool tests to interact with."""
    user = User(
        email="test@example.com",
        name="Test Customer",
        role=UserRole.CUSTOMER,
        is_active=True
    )
    db_session.add(user)
    
    tag_sale = Tag(name="sale")
    db_session.add(tag_sale)
    
    product1 = Product(
        name="Test Wireless Earbuds",
        description="Noise-cancelling wireless earbuds.",
        price=99.99,
        stock_quantity=50,
        category="Electronics",
        tags=[tag_sale]
    )
    product2 = Product(
        name="Test Coffee Mug",
        description="Ceramic mug, 12oz.",
        price=14.50,
        stock_quantity=0,
        category="Home & Kitchen"
    )
    db_session.add_all([product1, product2])
    db_session.commit()
    
    return {
        "user_id": user.id,
        "product_in_stock_id": product1.id,
        "product_out_of_stock_id": product2.id
    }


@pytest.fixture(scope="function")
def mock_config(monkeypatch):
    """
    Modifies the global config singleton in-place for tests.
    Monkeypatch automatically reverts these changes after the test.
    """
    from utils.config import config
    
    monkeypatch.setattr(config.agent, "max_tool_retries", 3)
    monkeypatch.setattr(config.agent, "guard_enabled", True)
    monkeypatch.setattr(config.agent, "sensitive_tool_names", ["checkout", "add_to_cart"])
    monkeypatch.setattr(config.system_context, "company_name", "TestStore")
    monkeypatch.setattr(config.system_context, "tone", "helpful and professional")
    monkeypatch.setattr(config.system_context, "system_prompt_template", "You are {company_name}, {tone}.")
    
    return config


@pytest.fixture(scope="function")
def clean_tool_registry():
    """Temporarily cleans TOOL_REGISTRY so `@agent_tool` tests don't bleed."""
    from agent.tooling import TOOL_REGISTRY
    original = TOOL_REGISTRY.copy()
    TOOL_REGISTRY.clear()
    
    yield
    
    TOOL_REGISTRY.clear()
    TOOL_REGISTRY.update(original)