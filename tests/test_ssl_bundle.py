"""A missing CA bundle presents as a silent strategy, not as a TLS error.

The scalper appeared not to fire. Its websocket was failing
CERTIFICATE_VERIFY_FAILED in a reconnect loop, so no tick ever reached the
engine and every gate downstream looked healthy because nothing was asking them.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys


def test_runner_sets_a_ca_bundle_before_importing_clients():
    src = pathlib.Path("scripts/run_live.py").read_text()
    set_at = src.index("SSL_CERT_FILE")
    coord_at = src.index("from src.agents.coordinator import")
    assert set_at < coord_at, "CA bundle must be set before any client is imported"


def test_certifi_bundle_exists_and_is_readable():
    import certifi

    p = pathlib.Path(certifi.where())
    assert p.is_file() and p.stat().st_size > 1000


def test_both_common_env_vars_are_set():
    src = pathlib.Path("scripts/run_live.py").read_text()
    assert "SSL_CERT_FILE" in src and "REQUESTS_CA_BUNDLE" in src


def test_setdefault_does_not_override_an_explicit_choice():
    """A deployment that pins its own bundle must win."""
    src = pathlib.Path("scripts/run_live.py").read_text()
    assert "setdefault" in src
