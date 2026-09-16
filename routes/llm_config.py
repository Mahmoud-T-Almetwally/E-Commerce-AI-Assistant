"""
Admin API for the LLM + agent runtime configuration (the playground backend).

    GET    /admin/llm-config        current settings (secrets never returned)
    PUT    /admin/llm-config        partial update of llm_config / agent
    POST   /admin/llm-config/test   dry-run: constructs the client, no API call
    POST   /admin/llm-config/reset  restore llm_config + agent to defaults

Sections other than llm_config/agent are read-only: embedding changes would
corrupt the vector store without a forced resync (content hashes don't detect
embedding-model changes), and flask/database/meta/rag are deployment concerns.
API keys are env-only — never accepted, never returned.

LLM changes take effect on the NEXT turn via the graph's config-fingerprint
cache (in-flight turns finish on the old settings); agent.* changes apply
per-invocation immediately. Mutating endpoints require the X-CSRFToken header.
"""

import importlib.util
import logging
import math
import os
import threading
from typing import Any, Dict

from flask import Blueprint, jsonify, request
from pydantic import ValidationError

from agent.providers import ModelFactory
from utils.auth import admin_required
from utils.config import (
    AgentRuntimeConfig,
    LLMConfig,
    PROVIDER_API_KEY_MAP,
    config,
    save_config,
)
from utils.extensions import limiter

llm_config_bp = Blueprint('llm_config', __name__)
logger = logging.getLogger(__name__)

_SAVE_LOCK = threading.Lock()

LLM_EDITABLE_FIELDS = frozenset({
    "provider", "model_name", "temperature", "max_tokens", "top_p", "seed",
    "max_retries", "timeout_seconds", "vision_capable",
})
AGENT_EDITABLE_FIELDS = frozenset({
    "guard_enabled", "guard_include_context", "max_tool_retries",
    "tool_retry_backoff_seconds", "status_events_enabled",
    "sensitive_tool_names", "confirmation_timeout_seconds",
    "max_upload_mb", "max_message_chars", "allowed_upload_extensions",
})
SECTION_FIELDS = {"llm_config": LLM_EDITABLE_FIELDS, "agent": AGENT_EDITABLE_FIELDS}
SECTION_MODELS = {"llm_config": LLMConfig, "agent": AgentRuntimeConfig}

PROVIDER_PACKAGES = {
    "openai": "langchain_openai",
    "anthropic": "langchain_anthropic",
    "groq": "langchain_groq",
    "google": "langchain_google_genai",
    "huggingface": "langchain_huggingface",
    "local": "langchain_ollama",
}
EMBEDDING_PROVIDERS = ("openai", "huggingface", "google", "local")


def _key_present(provider: str) -> bool:
    env_var = PROVIDER_API_KEY_MAP.get(provider.lower())
    if env_var is None:
        return True          # 'local' provider needs no key
    return bool(os.environ.get(env_var))


def _provider_status() -> Dict[str, Any]:
    """Provider availability map for the playground dropdowns."""
    statuses: Dict[str, Any] = {}
    for provider, package in PROVIDER_PACKAGES.items():
        try:
            installed = importlib.util.find_spec(package) is not None
        except (ImportError, ValueError):
            installed = False
        statuses[provider] = {"installed": installed,
                              "key_present": _key_present(provider)}
    return statuses


def _public_llm(llm: LLMConfig) -> Dict[str, Any]:
    data = llm.model_dump()
    data.pop("api_key", None)
    data["api_key_set"] = bool(llm.api_key) or _key_present(llm.provider)
    return data


def _public_embedding(embedding: Any) -> Dict[str, Any]:
    data = embedding.model_dump()
    data.pop("api_key", None)
    data["api_key_set"] = bool(embedding.api_key) or _key_present(embedding.provider)
    return data


def _config_payload() -> Dict[str, Any]:
    return {
        "llm_config": _public_llm(config.llm_config),
        "agent": config.agent.model_dump(),
        "read_only": {
            "embedding_config": _public_embedding(config.embedding_config),
            "embedding_providers": list(EMBEDDING_PROVIDERS),
            "system_context": config.system_context.model_dump(),
        },
        "providers": _provider_status(),
        "editable_fields": {name: sorted(fields)
                            for name, fields in SECTION_FIELDS.items()},
    }


def _validation_message(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors():
        loc = ".".join(str(item) for item in err.get("loc", ()))
        msg = err.get("msg", "invalid value")
        parts.append(f"{loc}: {msg}" if loc else str(msg))
    return "; ".join(parts) or "invalid values"


def _apply_changes(data: Dict[str, Any]):
    """
    Validate a partial {"llm_config": {...}, "agent": {...}} update against the
    live configuration and return a NEW AgentConfiguration to be saved.
    Raises ValueError with a user-facing message on any rejected input.
    The live config object is never mutated here.
    """
    unknown_sections = sorted(set(data) - set(SECTION_FIELDS))
    if unknown_sections:
        raise ValueError(
            "Unknown or read-only section(s): " + ", ".join(unknown_sections)
            + ". Editable sections: " + ", ".join(sorted(SECTION_FIELDS)) + ".")

    updated: Dict[str, Any] = {}
    for section, allowed_fields in SECTION_FIELDS.items():
        changes = data.get(section)
        if changes is None:
            continue
        if not isinstance(changes, dict):
            raise ValueError(f"Section '{section}' must be a JSON object.")
        unknown = sorted(set(changes) - allowed_fields)
        if unknown:
            hint = ""
            if "api_key" in unknown:
                hint = (" (API keys are managed exclusively via environment"
                        " variables)")
            raise ValueError(
                f"Unknown or read-only field(s) in '{section}': "
                + ", ".join(unknown) + hint + ".")
        merged = getattr(config, section).model_dump()
        merged.update(changes)
        try:
            updated[section] = SECTION_MODELS[section].model_validate(merged)
        except ValidationError as exc:
            raise ValueError(
                f"Invalid value(s) for '{section}': "
                f"{_validation_message(exc)}") from exc

    if "llm_config" in updated:
        temperature = updated["llm_config"].temperature
        if temperature is None or not math.isfinite(temperature):
            raise ValueError("Invalid value(s) for 'llm_config': "
                             "temperature must be a finite number.")

    new_config = config.model_copy(deep=True)
    for section, value in updated.items():
        object.__setattr__(new_config, section, value)
    return new_config


@llm_config_bp.route('', methods=['GET'])
@limiter.limit("60 per minute")
@admin_required
def get_config():
    """Current LLM/agent settings (secret-free) + provider availability."""
    return jsonify(_config_payload())


@llm_config_bp.route('', methods=['PUT'])
@limiter.limit("10 per minute")
@admin_required
def update_config():
    """Partial update of the llm_config / agent sections (hot-reloaded)."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Request body must be a JSON object."}), 400
    try:
        new_config = _apply_changes(data)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    try:
        with _SAVE_LOCK:
            save_config(new_config)
    except Exception:
        logger.exception("Failed to persist LLM/agent configuration via admin API.")
        return jsonify({"error": "Configuration validated but could not be saved."}), 500

    logger.info("LLM/agent configuration updated via admin API (provider=%s, model=%s).",
                config.llm_config.provider, config.llm_config.model_name)
    payload = _config_payload()
    payload["saved"] = True
    return jsonify(payload)


@llm_config_bp.route('/test', methods=['POST'])
@limiter.limit("10 per minute")
@admin_required
def test_config():
    """
    Dry-run a candidate configuration (or the current one on an empty body):
    constructs the provider client — package present, key resolvable, model
    name accepted by the constructor — WITHOUT making an API call. Never saves.
    """
    data = request.get_json(silent=True)
    if data is not None and not isinstance(data, dict):
        return jsonify({"error": "Request body must be a JSON object."}), 400
    if data:
        try:
            candidate = _apply_changes(data)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
    else:
        candidate = config

    try:
        ModelFactory.get_llm(candidate.llm_config)
    except Exception as exc:
        return jsonify({"ok": False,
                        "provider": candidate.llm_config.provider,
                        "model_name": candidate.llm_config.model_name,
                        "error": str(exc)})
    return jsonify({"ok": True,
                    "provider": candidate.llm_config.provider,
                    "model_name": candidate.llm_config.model_name,
                    "note": "Client constructed successfully; no API call was made."})


@llm_config_bp.route('/reset', methods=['POST'])
@limiter.limit("5 per minute")
@admin_required
def reset_config():
    """Restore the llm_config and agent sections to their pydantic defaults."""
    new_config = config.model_copy(deep=True)
    object.__setattr__(new_config, "llm_config", LLMConfig())
    object.__setattr__(new_config, "agent", AgentRuntimeConfig())
    try:
        with _SAVE_LOCK:
            save_config(new_config)
    except Exception:
        logger.exception("Failed to reset LLM/agent configuration.")
        return jsonify({"error": "Could not reset the configuration."}), 500

    logger.info("LLM/agent configuration reset to defaults via admin API.")
    payload = _config_payload()
    payload["saved"] = True
    return jsonify(payload)