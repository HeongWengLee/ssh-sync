"""Typer CLI entrypoint for ssh-sync."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

from sshsync import __version__
from sshsync.config import AppConfig, OptionsConfig, SSHConfig, SyncConfig, load_config
from sshsync.ssh_connection import SSHConnection
from sshsync.sync_engine import SyncEngine
from sshsync.utils import configure_logging


LOGGER = logging.getLogger(__name__)
app = typer.Typer(help="Bidirectional file synchronization over SSH/SFTP.")
console = Console()


def _build_runtime_config(
    config_file: Optional[Path],
    remote_host: Optional[str],
    remote_port: int,
    remote_user: Optional[str],
    remote_password: Optional[str],
    private_key: Optional[str],
    use_agent: bool,
    local_dir: Optional[Path],
    remote_dir: Optional[str],
    use_hash: bool,
) -> AppConfig:
    """Merge CLI flags over optional config file."""
    cfg = load_config(config_file) if config_file else None

    host = remote_host or (cfg.ssh.host if cfg else None)
    if not host:
        raise typer.BadParameter("Remote host is required via --remote-host or config file.")

    resolved_local_dir = str(local_dir) if local_dir else (cfg.sync.local_dir if cfg else None)
    resolved_remote_dir = remote_dir or (cfg.sync.remote_dir if cfg else None)
    if not resolved_local_dir or not resolved_remote_dir:
        raise typer.BadParameter("Both local and remote directories are required.")

    return AppConfig(
        ssh=SSHConfig(
            host=host,
            port=remote_port,
            user=remote_user,
            private_key=private_key,
            password=remote_password,
            use_agent=use_agent,
        ),
        sync=SyncConfig(
            local_dir=resolved_local_dir,
            remote_dir=resolved_remote_dir,
        ),
        options=OptionsConfig(
            use_hash=use_hash,
            delete_protection=True,
        ),
    )


def _override_cfg(
    cfg: AppConfig,
    remote_host: Optional[str],
    remote_port: int,
    remote_user: Optional[str],
    remote_password: Optional[str],
    private_key: Optional[str],
    use_agent: bool,
    local_dir: Optional[Path],
    remote_dir: Optional[str],
    use_hash: bool,
) -> AppConfig:
    """Override config-file values with explicit CLI flags."""
    if remote_host:
        cfg.ssh.host = remote_host
    cfg.ssh.port = remote_port or cfg.ssh.port
    if remote_user:
        cfg.ssh.user = remote_user
    if remote_password:
        cfg.ssh.password = remote_password
    if private_key:
        cfg.ssh.private_key = private_key
    cfg.ssh.use_agent = use_agent
    if local_dir:
        cfg.sync.local_dir = str(local_dir)
    if remote_dir:
        cfg.sync.remote_dir = remote_dir
    cfg.options.use_hash = use_hash
    return cfg


def _create_config(
    config_file: Optional[Path],
    remote_host: Optional[str],
    remote_port: int,
    remote_user: Optional[str],
    remote_password: Optional[str],
    private_key: Optional[str],
    use_agent: bool,
    local_dir: Optional[Path],
    remote_dir: Optional[str],
    use_hash: bool,
) -> AppConfig:
    """Create final runtime config."""
    if config_file and config_file.exists():
        cfg = load_config(config_file)
        return _override_cfg(
            cfg,
            remote_host,
            remote_port,
            remote_user,
            remote_password,
            private_key,
            use_agent,
            local_dir,
            remote_dir,
            use_hash,
        )
    return _build_runtime_config(
        config_file,
        remote_host,
        remote_port,
        remote_user,
        remote_password,
        private_key,
        use_agent,
        local_dir,
        remote_dir,
        use_hash,
    )


def _run_mode(
    mode: str,
    config_file: Optional[Path],
    remote_host: Optional[str],
    remote_port: int,
    remote_user: Optional[str],
    remote_password: Optional[str],
    private_key: Optional[str],
    use_agent: bool,
    local_dir: Optional[Path],
    remote_dir: Optional[str],
    use_hash: bool,
    delete_missing: bool,
    dry_run: bool,
    verbose: bool,
) -> None:
    """Create connections and execute requested sync mode."""
    configure_logging(verbose)

    cfg = _create_config(
        config_file,
        remote_host,
        remote_port,
        remote_user,
        remote_password,
        private_key,
        use_agent,
        local_dir,
        remote_dir,
        use_hash,
    )

    with SSHConnection(
        host=cfg.ssh.host,
        port=cfg.ssh.port,
        username=cfg.ssh.user,
        private_key=cfg.ssh.private_key,
        password=cfg.ssh.password,
        use_agent=cfg.ssh.use_agent,
    ) as connection:
        engine = SyncEngine(
            connection=connection,
            local_dir=Path(cfg.sync.local_dir),
            remote_dir=cfg.sync.remote_dir,
            use_hash=cfg.options.use_hash,
            dry_run=dry_run,
            delete_missing=delete_missing,
            console=console,
        )

        if mode == "pull":
            result = engine.run_pull()
        elif mode == "push":
            result = engine.run_push()
        elif mode == "sync":
            result = engine.run_sync()
        else:
            raise typer.BadParameter(f"Unsupported mode: {mode}")

        console.print("\nSync Summary")
        console.print("------------")
        console.print(f"Total scanned:    {result.total_scanned}")
        console.print(f"Ignored:          {result.skipped_ignored}")
        console.print(f"Uploaded:         {len(result.uploaded)}")
        console.print(f"Downloaded:       {len(result.downloaded)}")
        console.print(f"Conflicts:        {len(result.conflicts)}")
        console.print(f"Duration:         {result.duration_seconds:.3f}s")


common_options = {
    "config_file": typer.Option(None, "--config", help="Path to config YAML."),
    "remote_host": typer.Option(None, "--remote-host", help="Remote SSH host."),
    "remote_port": typer.Option(22, "--remote-port", help="Remote SSH port."),
    "remote_user": typer.Option(None, "--remote-user", help="Remote SSH username."),
    "remote_password": typer.Option(None, "--remote-password", help="Remote SSH password."),
    "private_key": typer.Option(None, "--private-key", help="Private key path for auth."),
    "use_agent": typer.Option(True, "--use-agent/--no-agent", help="Enable SSH agent authentication."),
    "local_dir": typer.Option(None, "--local-dir", help="Local directory path."),
    "remote_dir": typer.Option(None, "--remote-dir", help="Remote directory path."),
    "use_hash": typer.Option(False, "--use-hash", help="Use SHA256 verification."),
    "delete_missing": typer.Option(False, "--delete", help="Delete files that exist only on one side."),
    "dry_run": typer.Option(False, "--dry-run", help="Show actions without transfer."),
    "verbose": typer.Option(False, "--verbose", help="Enable debug logs."),
}


@app.command()
def pull(
    config_file: Optional[Path] = common_options["config_file"],
    remote_host: Optional[str] = common_options["remote_host"],
    remote_port: int = common_options["remote_port"],
    remote_user: Optional[str] = common_options["remote_user"],
    remote_password: Optional[str] = common_options["remote_password"],
    private_key: Optional[str] = common_options["private_key"],
    use_agent: bool = common_options["use_agent"],
    local_dir: Optional[Path] = common_options["local_dir"],
    remote_dir: Optional[str] = common_options["remote_dir"],
    use_hash: bool = common_options["use_hash"],
    delete_missing: bool = common_options["delete_missing"],
    dry_run: bool = common_options["dry_run"],
    verbose: bool = common_options["verbose"],
) -> None:
    """Pull files from remote host to local directory."""
    _run_mode(
        "pull",
        config_file,
        remote_host,
        remote_port,
        remote_user,
        remote_password,
        private_key,
        use_agent,
        local_dir,
        remote_dir,
        use_hash,
        delete_missing,
        dry_run,
        verbose,
    )


@app.command()
def push(
    config_file: Optional[Path] = common_options["config_file"],
    remote_host: Optional[str] = common_options["remote_host"],
    remote_port: int = common_options["remote_port"],
    remote_user: Optional[str] = common_options["remote_user"],
    remote_password: Optional[str] = common_options["remote_password"],
    private_key: Optional[str] = common_options["private_key"],
    use_agent: bool = common_options["use_agent"],
    local_dir: Optional[Path] = common_options["local_dir"],
    remote_dir: Optional[str] = common_options["remote_dir"],
    use_hash: bool = common_options["use_hash"],
    delete_missing: bool = common_options["delete_missing"],
    dry_run: bool = common_options["dry_run"],
    verbose: bool = common_options["verbose"],
) -> None:
    """Push files from local directory to remote host."""
    _run_mode(
        "push",
        config_file,
        remote_host,
        remote_port,
        remote_user,
        remote_password,
        private_key,
        use_agent,
        local_dir,
        remote_dir,
        use_hash,
        delete_missing,
        dry_run,
        verbose,
    )


@app.command()
def sync(
    config_file: Optional[Path] = common_options["config_file"],
    remote_host: Optional[str] = common_options["remote_host"],
    remote_port: int = common_options["remote_port"],
    remote_user: Optional[str] = common_options["remote_user"],
    remote_password: Optional[str] = common_options["remote_password"],
    private_key: Optional[str] = common_options["private_key"],
    use_agent: bool = common_options["use_agent"],
    local_dir: Optional[Path] = common_options["local_dir"],
    remote_dir: Optional[str] = common_options["remote_dir"],
    use_hash: bool = common_options["use_hash"],
    delete_missing: bool = common_options["delete_missing"],
    dry_run: bool = common_options["dry_run"],
    verbose: bool = common_options["verbose"],
) -> None:
    """Bidirectional synchronization between local and remote."""
    _run_mode(
        "sync",
        config_file,
        remote_host,
        remote_port,
        remote_user,
        remote_password,
        private_key,
        use_agent,
        local_dir,
        remote_dir,
        use_hash,
        delete_missing,
        dry_run,
        verbose,
    )


@app.callback(invoke_without_command=False)
def main() -> None:
    """CLI callback placeholder."""


def run() -> None:
    """Console script entry point."""
    app()


if __name__ == "__main__":
    run()

