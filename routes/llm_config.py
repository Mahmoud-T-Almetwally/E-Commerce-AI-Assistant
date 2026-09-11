from flask import Blueprint, jsonify, request
from pydantic import ValidationError

from utils.auth import admin_required
from utils.config import LLMConfig, config, save_config

bp = Blueprint("llm_config", __name__, url_prefix="/api/llm-config")

# Fields an admin may tune at runtime. `api_key` is deliberately excluded —
# secrets stay in the environment, never in the YAML file or this API.
MUTABLE_FIELDS = {
    "provider", "model_name", "temperature", "max_tokens",
    "top_p", "seed", "max_retries", "timeout_seconds", "vision_capable",
}


def _masked_dump() -> dict:
    data = config.llm_config.model_dump(mode="json")
    if data.get("api_key"):
        data["api_key"] = "********"
    return data


@bp.get("")
@admin_required
def get_llm_config():
    """Returns the effective LLM configuration (secrets masked)."""
    return jsonify(_masked_dump())


@bp.put("")
@admin_required
def update_llm_config():
    """
    Validates and applies new LLM parameters, persists them to config.yaml,
    and hot-reloads the process-wide config (effective on the next request).

    Note: `vision_capable` also gates chat file uploads — disabling it while
    users are mid-upload will reject new attachments.
    """
    body = request.get_json(silent=True) or {}
    unknown = set(body) - MUTABLE_FIELDS
    if unknown:
        return jsonify({"error": f"Unknown or read-only fields: {sorted(unknown)}"}), 400

    candidate = config.llm_config.model_dump()
    candidate.update(body)
    candidate["api_key"] = config.llm_config.api_key  # never overwritten via API

    try:
        updated = LLMConfig.model_validate(candidate)
    except ValidationError as exc:
        return jsonify({"error": "Invalid configuration.", "details": exc.errors()}), 422

    save_config(config.model_copy(update={"llm_config": updated}))
    return jsonify(_masked_dump())
