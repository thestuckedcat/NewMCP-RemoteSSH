from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(slots=True)
class SSHConfig:
    """SSH connection configuration for the persistent Paramiko client."""

    host: str
    port: int = 22
    username: str | None = None
    key_filename: str | None = None
    password: str | None = None
    passphrase: str | None = None
    connect_timeout: int = 10
    auth_timeout: int = 15
    banner_timeout: int = 15
    keepalive_interval: int = 30

    @classmethod
    def from_env(cls) -> "SSHConfig":
        host = os.getenv("SSH_HOST", "").strip()
        if not host:
            raise ValueError("SSH_HOST is required")

        return cls(
            host=host,
            port=int(os.getenv("SSH_PORT", "22")),
            username=os.getenv("SSH_USERNAME") or None,
            key_filename=os.getenv("SSH_KEY_FILENAME") or None,
            password=os.getenv("SSH_PASSWORD") or None,
            passphrase=os.getenv("SSH_PASSPHRASE") or None,
            connect_timeout=int(os.getenv("SSH_CONNECT_TIMEOUT", "10")),
            auth_timeout=int(os.getenv("SSH_AUTH_TIMEOUT", "15")),
            banner_timeout=int(os.getenv("SSH_BANNER_TIMEOUT", "15")),
            keepalive_interval=int(os.getenv("SSH_KEEPALIVE_INTERVAL", "30")),
        )
