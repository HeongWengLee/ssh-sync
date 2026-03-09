"""Configuration loading utilities for ssh-sync."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(slots=True)
class SSHConfig:
    """SSH connection configuration."""

    host: str
    port: int = 22
    user: str | None = None
    private_key: str | None = None
    password: str | None = None
    use_agent: bool = True


@dataclass(slots=True)
class SyncConfig:
    """Sync location configuration."""

    local_dir: str
    remote_dir: str


@dataclass(slots=True)
class OptionsConfig:
    """General sync options."""

    use_hash: bool = False
    delete_protection: bool = True


@dataclass(slots=True)
class AppConfig:
    """Root application configuration."""

    ssh: SSHConfig
    sync: SyncConfig
    options: OptionsConfig


def load_config(path: Path) -> AppConfig:
    """Load YAML config from disk and parse into dataclasses."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    ssh_raw = raw.get("ssh", {})
    sync_raw = raw.get("sync", {})
    opt_raw = raw.get("options", {})

    ssh = SSHConfig(
        host=ssh_raw.get("host", ""),
        port=int(ssh_raw.get("port", 22)),
        user=ssh_raw.get("user"),
        private_key=ssh_raw.get("private_key"),
        password=ssh_raw.get("password"),
        use_agent=bool(ssh_raw.get("use_agent", True)),
    )

    sync = SyncConfig(
        local_dir=sync_raw.get("local_dir", "./"),
        remote_dir=sync_raw.get("remote_dir", "/"),
    )

    options = OptionsConfig(
        use_hash=bool(opt_raw.get("use_hash", False)),
        delete_protection=bool(opt_raw.get("delete_protection", True)),
    )

    return AppConfig(ssh=ssh, sync=sync, options=options)

