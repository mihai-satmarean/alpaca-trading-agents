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

# Every notification is also written here, whatever transport carried it and
# whether or not it arrived. ntfy.sh retains messages for about 12 hours, so
# it cannot answer "what did the system say overnight", and it records only
# what succeeded -- a failed alert leaves no trace anywhere, which is the
# failure mode that hides an outage for days. The journal is the durable
# record; ntfy is the delivery channel.
JOURNAL_PATH = os.environ.get(
    "NOTIFY_JOURNAL", os.path.join(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))), "logs", "notifications.jsonl"))
JOURNAL_MAX_BYTES = 2_000_000
PRIORITY = {"min": 1, "low": 2, "default": 3, "high": 4, "urgent": 5}

# ntfy-bridge maps severity through its own table and accepts only these three.
# Anything else silently degrades to "default", so map ours before publishing.
BRIDGE_SEVERITY = {"min": "low", "low": "low", "default": "default",
                   "high": "urgent", "urgent": "urgent"}


def _journal(entry: dict) -> None:
    """Append one record. Never raises: monitoring must not break trading.

    Trimmed by rewriting the tail rather than rotating, because the readers
    open a fixed path and a rotation would silently hide recent history from
    them until someone noticed the panel had gone quiet.
    """
    try:
        os.makedirs(os.path.dirname(JOURNAL_PATH), exist_ok=True)
        with open(JOURNAL_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
        if os.path.getsize(JOURNAL_PATH) > JOURNAL_MAX_BYTES:
            with open(JOURNAL_PATH, "r", encoding="utf-8") as fh:
                keep = fh.readlines()[-2000:]
            with open(JOURNAL_PATH, "w", encoding="utf-8") as fh:
                fh.writelines(keep)
    except Exception:
        log.debug("notification journal write failed", exc_info=True)


def read_journal(limit: int = 50) -> list[dict]:
    """Most recent notifications, newest first. Never raises."""
    try:
        with open(JOURNAL_PATH, "r", encoding="utf-8") as fh:
            lines = fh.readlines()[-limit:]
    except Exception:
        return []
    out = []
    for ln in reversed(lines):
        try:
            out.append(json.loads(ln))
        except Exception:
            continue
    return out


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
    import datetime as _dt
    entry = {
        "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "title": title[:200], "message": message, "severity": severity,
        "tags": tags or [], "topic": topic, "transport": None, "delivered": False,
    }

    if os.environ.get("SNS_TOPIC_ARN"):
        if publish_sns(title, message, severity=severity, tags=tags):
            entry.update(transport="sns", delivered=True)
            _journal(entry)
            return True
        log.warning("SNS publish failed; falling back to a direct ntfy post")
        entry["sns_failed"] = True

    try:
        req = urllib.request.Request(
            "https://ntfy.sh", data=body, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as r:
            ok = 200 <= r.status < 300
            entry.update(transport="ntfy", delivered=ok, status=r.status)
            _journal(entry)
            return ok
    except Exception as exc:
        log.warning("ntfy publish failed for %r", title, exc_info=True)
        entry.update(transport="ntfy", delivered=False, error=str(exc)[:200])
        _journal(entry)
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
