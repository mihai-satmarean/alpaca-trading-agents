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

# ntfy-bridge maps severity through its own table and accepts only these three.
# Anything else silently degrades to "default", so map ours before publishing.
BRIDGE_SEVERITY = {"min": "low", "low": "low", "default": "default",
                   "high": "urgent", "urgent": "urgent"}


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

    # SNS is preferred when configured, and used INSTEAD of the direct post
    # rather than alongside it: ntfy-bridge delivers to this same topic, so
    # running both sinks sends every alert twice. SNS additionally retries,
    # which a fire-and-forget POST does not.
    if os.environ.get("SNS_TOPIC_ARN"):
        if publish_sns(title, message, severity=severity, tags=tags):
            return True
        log.warning("SNS publish failed; falling back to a direct ntfy post")

    try:
        req = urllib.request.Request(
            "https://ntfy.sh", data=body, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as r:
            return 200 <= r.status < 300
    except Exception:
        log.warning("ntfy publish failed for %r", title, exc_info=True)
        return False


def publish_sns(title: str, message: str, *, severity: str = "default",
                tags: list[str] | None = None, fields: list[tuple[str, str]] | None = None,
                topic_arn: str | None = None) -> bool:
    """Also publish to SNS, when a topic is configured.

    Optional on purpose. The repo is public for judging and boto3 needs AWS
    credentials that nobody else on the team or on the judging panel has, so
    ntfy stays the default path and this is opt-in via SNS_TOPIC_ARN. A missing
    topic is a no-op, not an error.

    The envelope shape is ntfy-bridge's contract: it JSON.parses the SNS
    Message, so a plain-text publish fails there rather than here.
    """
    topic_arn = topic_arn or os.environ.get("SNS_TOPIC_ARN")
    if not topic_arn:
        return False

    envelope = {
        "source": "alpaca-hackathon",
        "severity": BRIDGE_SEVERITY.get(severity, "default"),
        "subject": title[:250],
        "text": message,
        "tags": tags or [],
    }
    if fields:
        envelope["fields"] = [[str(k), str(v)] for k, v in fields]

    try:
        import boto3

        boto3.client("sns", region_name=os.environ.get("AWS_REGION", "us-east-1")).publish(
            TopicArn=topic_arn,
            Subject=title[:100],
            Message=json.dumps(envelope),
        )
        return True
    except Exception:
        log.warning("SNS publish failed for %r", title, exc_info=True)
        return False


def fmt_money(x: float) -> str:
    return f"{'-' if x < 0 else '+'}${abs(x):,.2f}"
