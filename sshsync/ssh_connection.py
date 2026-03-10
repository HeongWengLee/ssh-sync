"""SSH connection management using Paramiko."""

from __future__ import annotations

import logging
from pathlib import Path

import paramiko


LOGGER = logging.getLogger(__name__)


class InteractiveTrustPolicy(paramiko.MissingHostKeyPolicy):
    """Prompt user to trust unknown host keys at runtime."""

    def __init__(self, known_hosts_path: Path | None = None) -> None:
        self.known_hosts_path = known_hosts_path or (Path.home() / ".ssh" / "known_hosts")

    def missing_host_key(self, client: paramiko.SSHClient, hostname: str, key: paramiko.PKey) -> None:
        fingerprint = ":".join(f"{b:02x}" for b in key.get_fingerprint())
        answer = input(
            f"Unknown SSH host '{hostname}' ({key.get_name()} {fingerprint}). Trust and continue? [y/N]: "
        ).strip().lower()

        if answer not in {"y", "yes"}:
            raise paramiko.SSHException(f"User rejected unknown host key for {hostname}")

        host_keys = client.get_host_keys()
        host_keys.add(hostname, key.get_name(), key)
        try:
            self.known_hosts_path.parent.mkdir(parents=True, exist_ok=True)
            client.save_host_keys(str(self.known_hosts_path))
        except OSError as exc:
            LOGGER.warning("Unable to persist known_hosts entry to %s: %s", self.known_hosts_path, exc)


class SSHConnection:
    """Manage SSH and SFTP sessions with multiple auth methods."""

    def __init__(
        self,
        host: str,
        port: int = 22,
        username: str | None = None,
        private_key: str | None = None,
        password: str | None = None,
        use_agent: bool = True,
        timeout: int = 20,
        insecure: bool = False,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.private_key = private_key
        self.password = password
        self.use_agent = use_agent
        self.timeout = timeout
        self.insecure = insecure
        self.client: paramiko.SSHClient | None = None
        self.sftp: paramiko.SFTPClient | None = None

    def connect(self) -> None:
        """Open SSH and SFTP sessions."""
        if self.client is not None:
            return

        client = paramiko.SSHClient()
        client.load_system_host_keys()
        if self.insecure:
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        else:
            client.set_missing_host_key_policy(InteractiveTrustPolicy())

        pkey = None
        if self.private_key:
            key_path = Path(self.private_key).expanduser()
            pkey = paramiko.PKey.from_path(str(key_path))

        LOGGER.debug("Connecting SSH to %s:%s", self.host, self.port)
        try:
            client.connect(
                hostname=self.host,
                port=self.port,
                username=self.username,
                password=self.password,
                pkey=pkey,
                allow_agent=self.use_agent,
                look_for_keys=self.use_agent,
                timeout=self.timeout,
            )
        except paramiko.BadHostKeyException as exc:
            raise RuntimeError(f"Host key verification failed for {self.host}:{self.port}") from exc
        except paramiko.SSHException as exc:
            raise RuntimeError(f"SSH connection failed for {self.host}:{self.port}: {exc}") from exc

        self.client = client
        self.sftp = client.open_sftp()

    def close(self) -> None:
        """Close active SFTP and SSH sessions."""
        if self.sftp is not None:
            self.sftp.close()
            self.sftp = None
        if self.client is not None:
            self.client.close()
            self.client = None

    def exec_command(self, command: str) -> tuple[str, str, int]:
        """Execute command on remote host and return stdout, stderr, code."""
        if self.client is None:
            raise RuntimeError("SSH client is not connected")

        stdin, stdout, stderr = self.client.exec_command(command)
        _ = stdin
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        code = stdout.channel.recv_exit_status()
        return out, err, code

    def __enter__(self) -> "SSHConnection":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[override]
        _ = (exc_type, exc, tb)
        self.close()

