"""Shared compiler/runtime contract for packaged deployment endpoints."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

RUNTIME_CONFIG_NAME = "hub.runtime-config.json"
RUNTIME_CONFIG_SCHEMA_VERSION = 1

# Keep this pattern in sync with the source config's JSON schema. URL parsing
# below additionally checks IPv6 syntax and the numeric port range.
HTTPS_ORIGIN_PATTERN = r"^https://(?:[A-Za-z0-9.-]+|\[[0-9A-Fa-f:.]+\])(?::[0-9]+)?/?$"


def normalize_https_origin(value: str) -> str:
    """Validate a deployment origin without credentials or URL suffixes."""

    message = "must be an HTTPS origin without credentials, path, query, or fragment"
    if re.fullmatch(HTTPS_ORIGIN_PATTERN, value) is None:
        raise ValueError(message)
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError(message) from exc
    if not parsed.hostname or (port is not None and port == 0):
        raise ValueError(message)
    return value.removesuffix("/")
