"""API-key authentication and project resolution.

Keys are never stored in plaintext: config holds SHA-256 hex digests and the
gateway compares digests with a constant-time comparison across all projects.
"""

from __future__ import annotations

import hashlib
import hmac
import re

from cost_firewall.config import Config, ProjectConfig
from cost_firewall.errors import AuthError

_BEARER_RE = re.compile(r"^Bearer\s+(\S+)$", re.IGNORECASE)


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def extract_bearer(authorization: str | None) -> str:
    if not authorization:
        raise AuthError("Missing Authorization header. Use: Authorization: Bearer <key>.")
    match = _BEARER_RE.match(authorization.strip())
    if not match:
        raise AuthError("Malformed Authorization header. Use: Authorization: Bearer <key>.")
    return match.group(1)


class Authenticator:
    def __init__(self, config: Config):
        self._projects = config.projects

    def resolve(self, authorization: str | None) -> ProjectConfig:
        key = extract_bearer(authorization)
        digest = hash_api_key(key)
        matched: ProjectConfig | None = None
        for project in self._projects:
            for candidate in project.api_key_hashes:
                # Compare every candidate so timing does not reveal which project matched.
                if hmac.compare_digest(candidate, digest):
                    matched = project
        if matched is None:
            raise AuthError("Invalid API key.")
        return matched
