"""ntfy notifications for strategy monitoring.

Publishes to the topic already used by Frank's other trading systems, so the
hackathon agent lands in the same place as everything else on his phone.
"""

from __future__ import annotations

import json
import logging
import os
import ssl
import urllib.request

try:  # certifi ships with alpaca-py; some Python builds have no system CA bundle
    import certifi

    _SSL_CTX: ssl.SSLContext | None = ssl.create_default_context(cafile=certifi.where())
except Exception:  # pragma: no cover
    _SSL_CTX = None

log = logging.getLogger(__name__)

DEFAULT_TOPIC = "frank-trading-a0372d5e65"
PRIORITY = {"min": 1, "low": 2, "default": 3, "high": 4, "urgent": 5}


def notify(title: str, message: str, *, severity: str = "default",
           tags: list[str] | None = None, topic: str | None = None,
           timeout: float = 10.0) -> bool:
    """Publish one notification. Returns True on success, never raises.

    Monitoring must not be able to take down trading, so every failure is
    swallowed and logged. The JSON publish API rejects a string priority even
    when it contains a digit, so severity is mapped to a bare integer.
    """
    topic = topic or os.environ.get("NTFY_TOPIC", DEFAULT_TOPIC)
    body = json.dumps({
        "topic": topic,
        "title": title[:200],
        "message": message,
        "priority": PRIORITY.get(severity, 3),
        "tags": tags or [],
        "markdown": True,
    }).encode()

    try:
        req = urllib.request.Request(
            "https://ntfy.sh", data=body, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as r:
            return 200 <= r.status < 300
    except Exception:
        log.warning("ntfy publish failed for %r", title, exc_info=True)
        return False


def fmt_money(x: float) -> str:
    return f"{'-' if x < 0 else '+'}${abs(x):,.2f}"
