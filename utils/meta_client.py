"""
Thin Facebook Messenger (Graph API) client for the webhook transport.

Design constraints:
  * NEVER raises into the caller — every public method logs failures and
    degrades gracefully. A failed send must not break an agent turn.
  * Reads config.meta_config at CALL time (hot-reload friendly for the
    non-secret settings; secrets are env-sourced anyway).
  * Bounded retries for transient failures (network errors, 5xx, 429, and
    Graph rate-limit error codes); genuine client errors (bad payload) fail
    fast so we don't spam a broken request.
"""

import logging
import time
from typing import Any, Dict, List, Optional

import requests

from utils.config import config

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT = 15          # seconds per HTTP attempt
_MAX_RETRIES = 2               # 3 attempts total
_RETRY_BACKOFF_SECONDS = 1.0
_RETRYABLE_ERROR_CODES = {4, 613, 80004}   # rate limiting / too many sends

TEXT_LIMIT = 2000              # Messenger regular text message cap
TEMPLATE_TEXT_LIMIT = 640      # button template text cap
ELEMENTS_PER_TEMPLATE = 10     # generic template element cap


def chunk_text(text: str, limit: int = TEXT_LIMIT) -> List[str]:
    """Split long text into Messenger-sized chunks, preferring paragraph,
    then line, then space boundaries; hard-cuts as a last resort."""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    chunks: List[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        window = remaining[:limit]
        cut = max(window.rfind("\n\n"), window.rfind("\n"), window.rfind(" "))
        if cut < limit // 2:
            cut = limit
        piece = remaining[:cut].rstrip()
        if not piece:                      # pathological: all whitespace run
            piece = remaining[:limit]
            cut = limit
        chunks.append(piece)
        remaining = remaining[cut:].lstrip()
    return chunks


class MetaMessenger:
    """Stateless Graph API adapter — safe to share across background threads."""

    def _url(self, path: str) -> str:
        version = config.meta_config.graph_api_version or "v21.0"
        return f"https://graph.facebook.com/{version}/{path}"

    def _request(self, method: str, path: str, *, params=None,
                 json_body=None) -> Optional[Dict[str, Any]]:
        query = dict(params or {})
        query["access_token"] = config.meta_config.page_access_token or ""
        last_error = "unknown error"
        for attempt in range(_MAX_RETRIES + 1):
            try:
                response = requests.request(
                    method, self._url(path), params=query, json=json_body,
                    timeout=_REQUEST_TIMEOUT)
            except requests.RequestException as exc:
                last_error = repr(exc)
                retryable = True
            else:
                try:
                    body = response.json() if response.content else {}
                except ValueError:
                    body = {}
                error = body.get("error") if isinstance(body, dict) else None
                if response.status_code < 400 and error is None:
                    return body if isinstance(body, dict) else {}
                message = error.get("message") if isinstance(error, dict) else None
                last_error = f"HTTP {response.status_code}: {message or response.text[:200]}"
                code = error.get("code") if isinstance(error, dict) else None
                retryable = (response.status_code >= 500
                             or response.status_code == 429
                             or code in _RETRYABLE_ERROR_CODES)
                if not retryable:
                    break
            if attempt < _MAX_RETRIES:
                time.sleep(_RETRY_BACKOFF_SECONDS * (attempt + 1))
        logger.warning("Graph API %s %s failed: %s", method, path, last_error)
        return None

    def _send_payload(self, payload: Dict[str, Any]) -> bool:
        return self._request("POST", "me/messages", json_body=payload) is not None

    def _sender_action(self, psid: str, action: str) -> None:
        self._send_payload({"recipient": {"id": psid}, "sender_action": action})

    def mark_seen(self, psid: str) -> None:
        """Shows the 'Seen' receipt as soon as we dequeue an item."""
        self._sender_action(psid, "mark_seen")

    def set_typing(self, psid: str, on: bool = True) -> None:
        self._sender_action(psid, "typing_on" if on else "typing_off")

    def send_text(self, psid: str, text: str) -> None:
        """Send text, automatically chunked to Messenger's size limit."""
        for chunk in chunk_text(text):
            self._send_payload({
                "recipient": {"id": psid},
                "messaging_type": "RESPONSE",
                "message": {"text": chunk},
            })

    def send_generic(self, psid: str, elements: List[Dict[str, Any]]) -> None:
        """Send generic-template carousels (batched at 10 cards per message)."""
        elements = [e for e in elements if isinstance(e, dict)]
        for start in range(0, len(elements), ELEMENTS_PER_TEMPLATE):
            batch = elements[start:start + ELEMENTS_PER_TEMPLATE]
            self._send_payload({
                "recipient": {"id": psid},
                "messaging_type": "RESPONSE",
                "message": {"attachment": {
                    "type": "template",
                    "payload": {"template_type": "generic", "elements": batch},
                }},
            })

    def send_buttons(self, psid: str, text: str, buttons: List[Dict[str, Any]]) -> None:
        """Send a button template (≤3 buttons, 640-char text)."""
        text = (text or "").strip() or "Approve this action?"
        self._send_payload({
            "recipient": {"id": psid},
            "messaging_type": "RESPONSE",
            "message": {"attachment": {
                "type": "template",
                "payload": {
                    "template_type": "button",
                    "text": text[:TEMPLATE_TEXT_LIMIT],
                    "buttons": buttons[:3],
                },
            }},
        })

    def send_confirmation(self, psid: str, message: str, request_id: str) -> None:
        """The Messenger equivalent of get_user_confirmation: Accept/Decline."""
        self.send_buttons(psid, message, [
            {"type": "postback", "title": "Accept",
             "payload": f"CONFIRM:{request_id}:ACCEPT"},
            {"type": "postback", "title": "Decline",
             "payload": f"CONFIRM:{request_id}:DECLINE"},
        ])

    def get_profile(self, psid: str) -> Dict[str, Any]:
        """Best-effort profile fetch for auto-provisioning names."""
        data = self._request("GET", psid, params={"fields": "first_name,last_name"})
        return data if isinstance(data, dict) else {}

    def setup_messenger_profile(self, profile: Dict[str, Any]) -> bool:
        return self._request("POST", "me/messenger_profile",
                             json_body=profile) is not None


messenger = MetaMessenger()