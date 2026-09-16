"""Unit tests for the Messenger webhook transport's pure logic:
signature verification, postback parsing, text chunking, dedup."""

import hashlib
import hmac as hmac_module

import pytest

from routes import webhook
from routes.webhook import _mark_seen, _verify_signature, parse_postback_payload
from utils.config import config
from utils.meta_client import chunk_text


@pytest.fixture(autouse=True)
def _clean_dedup_store():
    webhook._SEEN_EVENTS.clear()
    yield
    webhook._SEEN_EVENTS.clear()


# ---------- chunking ----------

def test_chunk_text_short_and_empty():
    assert chunk_text("") == []
    assert chunk_text("hello") == ["hello"]
    assert chunk_text("x" * 2000) == ["x" * 2000]


def test_chunk_text_hard_cuts_unbroken_tokens():
    assert chunk_text("a" * 5000, limit=2000) == ["a" * 2000, "a" * 2000, "a" * 1000]


def test_chunk_text_prefers_paragraph_breaks():
    text = "para one\n\n" * 300            # 3000 chars, breaks every 10
    chunks = chunk_text(text, limit=100)
    assert len(chunks) >= 10
    assert all(len(c) <= 100 for c in chunks)
    assert chunks[0].startswith("para one")


def test_chunk_text_never_loses_content():
    text = ("Word " * 900).strip()         # 4499 chars with spaces
    chunks = chunk_text(text, limit=500)
    assert all(len(c) <= 500 for c in chunks)
    # every original word appears exactly once across chunks
    words = " ".join(chunks).split()
    assert words == text.split()


# ---------- signature verification ----------

def test_verify_signature_accepts_valid_hmac(monkeypatch):
    monkeypatch.setattr(config.meta_config, "app_secret", "test-secret")
    body = b'{"object":"page"}'
    signature = "sha256=" + hmac_module.new(
        b"test-secret", body, hashlib.sha256).hexdigest()
    assert _verify_signature(body, signature) is True


def test_verify_signature_rejects_bad_input(monkeypatch):
    monkeypatch.setattr(config.meta_config, "app_secret", "test-secret")
    body = b'{"object":"page"}'
    assert _verify_signature(body, "sha256=" + "0" * 64) is False    # wrong digest
    assert _verify_signature(body, None) is False                    # missing header
    assert _verify_signature(body, "sha1=abc") is False              # wrong scheme
    assert _verify_signature(b"", "sha256=" + "0" * 64) is False     # empty body


def test_verify_signature_fails_closed_without_secret(monkeypatch):
    monkeypatch.setattr(config.meta_config, "app_secret", None)
    digest = hashlib.sha256(b"{}").hexdigest()
    assert _verify_signature(b"{}", "sha256=" + digest) is False


# ---------- postback parsing ----------

def test_parse_confirm_payload():
    assert parse_postback_payload("CONFIRM:abc-123:ACCEPT") == {
        "request_id": "abc-123", "accepted": True}
    assert parse_postback_payload("CONFIRM:abc-123:DECLINE") == {
        "request_id": "abc-123", "accepted": False}


def test_parse_confirm_payload_rejects_malformed():
    assert parse_postback_payload("CONFIRM:abc:ACCEPT:extra") is None
    assert parse_postback_payload("CONFIRM:abc:MAYBE") is None
    assert parse_postback_payload("NEW_CHAT") is None
    assert parse_postback_payload("") is None


# ---------- dedup ----------

def test_mark_seen_deduplicates():
    assert _mark_seen("page1:mid1") is True
    assert _mark_seen("page1:mid1") is False     # redelivery
    assert _mark_seen("page1:mid2") is True
    assert _mark_seen("page2:mid1") is True