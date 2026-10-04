#!/usr/bin/env python3
"""Safe ntfy notification dispatcher with automatic secret scrubbing."""
from __future__ import annotations

import os
from typing import Any
import requests

from dossier import sanitize_secrets


def send(title: str, message: str, *, click: str | None = None, actions: list[dict[str, Any]] | None = None) -> bool:
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        return False

    safe_title = sanitize_secrets(title)
    safe_message = sanitize_secrets(message)
    payload: dict[str, Any] = {
        "topic": topic,
        "title": safe_title,
        "message": safe_message,
        "priority": 4,
        "tags": ["github", "robot_face"],
    }
    if click:
        payload["click"] = sanitize_secrets(click)
    if actions:
        clean_actions = []
        for a in actions:
            clean_a = dict(a)
            if "url" in clean_a:
                clean_a["url"] = sanitize_secrets(clean_a["url"])
            if "label" in clean_a:
                clean_a["label"] = sanitize_secrets(clean_a["label"])
            clean_actions.append(clean_a)
        payload["actions"] = clean_actions

    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    try:
        r = requests.post(server, json=payload, timeout=20)
        return 200 <= r.status_code < 300
    except requests.RequestException:
        return False
