"""Main synchronization orchestrator."""

from __future__ import annotations

import difflib
import logging
import posixpath
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from rich.console import Console

from sshsync.diff_engine import SyncPlan, build_pull_plan, build_push_plan, build_sync_plan
from sshsync.metadata import FileMetadata
from sshsync.scanner import load_ignore_patterns, scan_local_tree, scan_remote_tree
from sshsync.ssh_connection import SSHConnection
from sshsync.transfer import download_file, upload_file
from sshsync.utils import ensure_parent


LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class SyncResult:
    """Summary counters for one sync execution."""

    uploaded: list[PurePosixPath] = field(default_factory=list)
    downloaded: list[PurePosixPath] = field(default_factory=list)
    skipped: list[PurePosixPath] = field(default_factory=list)
    conflicts: list[PurePosixPath] = field(default_factory=list)


class SyncEngine:
    """Coordinates scanning, planning, conflict resolution, and transfer."""

    def __init__(
        self,
        connection: SSHConnection,
        local_dir: Path,
        remote_dir: str,
        use_hash: bool = False,
        dry_run: bool = False,
        console: Console | None = None,
    ) -> None:
        self.connection = connection
        self.local_dir = local_dir.expanduser().resolve()
        self.remote_dir = posixpath.normpath(remote_dir)
        self.use_hash = use_hash
        self.dry_run = dry_run
        self.console = console or Console()

    def run_pull(self) -> SyncResult:
        """Execute pull mode (remote -> local)."""
        plan, _local_entries, _remote_entries = self._build_plan("pull")
        result = SyncResult()
        self._apply_downloads(plan.download, result)
        self._report_skips(plan.skip)
        result.skipped.extend(plan.skip)
        return result

    def run_push(self) -> SyncResult:
        """Execute push mode (local -> remote)."""
        plan, _local_entries, _remote_entries = self._build_plan("push")
        result = SyncResult()
        self._apply_uploads(plan.upload, result)
        self._report_skips(plan.skip)
        result.skipped.extend(plan.skip)
        return result

    def run_sync(self) -> SyncResult:
        """Execute bidirectional sync mode."""
        plan, local_entries, remote_entries = self._build_plan("sync")
        result = SyncResult()

        self._apply_uploads(plan.upload, result)
        self._apply_downloads(plan.download, result)

        for path in plan.conflicts:
            result.conflicts.append(path)
            choice = self._resolve_conflict(path, local_entries, remote_entries)
            if choice == "remote":
                self._apply_downloads([path], result)
            elif choice == "local":
                self._apply_uploads([path], result)
            elif choice == "both":
                self._keep_both(path)
                result.downloaded.append(path)
                result.uploaded.append(path)
            elif choice == "skip":
                result.skipped.append(path)

        self._report_skips(plan.skip)
        result.skipped.extend(plan.skip)
        return result

    def _build_plan(
        self,
        mode: str,
    ) -> tuple[SyncPlan, dict[PurePosixPath, FileMetadata], dict[PurePosixPath, FileMetadata]]:
        """Scan trees and compute plan for a specific mode."""
        ignore_patterns = load_ignore_patterns(self.local_dir)
        local_entries = scan_local_tree(self.local_dir, self.use_hash, ignore_patterns)
        remote_entries = scan_remote_tree(self.connection, self.remote_dir, self.use_hash, ignore_patterns)

        if mode == "pull":
            plan = build_pull_plan(local_entries, remote_entries, self.use_hash)
        elif mode == "push":
            plan = build_push_plan(local_entries, remote_entries, self.use_hash)
        elif mode == "sync":
            plan = build_sync_plan(local_entries, remote_entries, self.use_hash)
        else:
            raise ValueError(f"Unsupported mode: {mode}")

        return plan, local_entries, remote_entries

    def _apply_downloads(self, items: list[PurePosixPath], result: SyncResult) -> None:
        """Apply planned downloads with dry-run support."""
        for path in items:
            if self.dry_run:
                self.console.print(f"+ download {path.as_posix()}")
                result.downloaded.append(path)
                continue
            download_file(self.connection, self.remote_dir, self.local_dir, path, dry_run=False)
            result.downloaded.append(path)

    def _apply_uploads(self, items: list[PurePosixPath], result: SyncResult) -> None:
        """Apply planned uploads with dry-run support."""
        for path in items:
            if self.dry_run:
                self.console.print(f"+ upload {path.as_posix()}")
                result.uploaded.append(path)
                continue
            upload_file(self.connection, self.local_dir, self.remote_dir, path, dry_run=False)
            result.uploaded.append(path)

    def _resolve_conflict(
        self,
        path: PurePosixPath,
        local_entries: dict[PurePosixPath, FileMetadata],
        remote_entries: dict[PurePosixPath, FileMetadata],
    ) -> str:
        """Prompt user to resolve conflict for one path."""
        self.console.print(f"Conflict detected: {path.as_posix()}")
        self.console.print("1) Use remote version")
        self.console.print("2) Use local version")
        self.console.print("3) Keep both versions")
        self.console.print("4) Show diff")
        self.console.print("5) Skip")

        while True:
            choice = self.console.input("Select an option [1-5]: ").strip()
            if choice == "1":
                return "remote"
            if choice == "2":
                return "local"
            if choice == "3":
                return "both"
            if choice == "4":
                _ = (local_entries, remote_entries)
                self._show_diff(path)
                continue
            if choice == "5":
                return "skip"
            self.console.print("Invalid option. Choose 1-5.")

    def _show_diff(self, relative_path: PurePosixPath) -> None:
        """Display text diff for local and remote versions when available."""
        if self.connection.sftp is None:
            raise RuntimeError("SFTP session is not connected")

        local_path = self.local_dir / Path(relative_path.as_posix())
        remote_path = posixpath.join(self.remote_dir, relative_path.as_posix())

        try:
            local_text = local_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            local_text = ["<unable to read local text>"]

        try:
            with self.connection.sftp.open(remote_path, "r") as handle:
                remote_text = handle.read().decode("utf-8", errors="replace").splitlines()
        except OSError:
            remote_text = ["<unable to read remote text>"]

        diff = difflib.unified_diff(
            local_text,
            remote_text,
            fromfile=f"local/{relative_path.as_posix()}",
            tofile=f"remote/{relative_path.as_posix()}",
            lineterm="",
        )
        rendered = "\n".join(diff) or "No textual differences to display."
        self.console.print(rendered)

    def _report_skips(self, items: list[PurePosixPath]) -> None:
        """Print skipped files in dry-run mode."""
        if not self.dry_run:
            return
        for path in items:
            self.console.print(f"- skip {path.as_posix()}")

    def _keep_both(self, relative_path: PurePosixPath) -> None:
        """Keep both local and remote versions as `.local` and `.remote` files."""
        if self.connection.sftp is None:
            raise RuntimeError("SFTP session is not connected")

        remote_origin = posixpath.join(self.remote_dir, relative_path.as_posix())
        local_origin = self.local_dir / Path(relative_path.as_posix())

        local_copy = local_origin.with_name(f"{local_origin.stem}.local")
        remote_copy_local = local_origin.with_name(f"{local_origin.stem}.remote")

        if self.dry_run:
            self.console.print(f"+ keep-both {relative_path.as_posix()} -> {local_copy.name}, {remote_copy_local.name}")
            return

        ensure_parent(local_copy)
        ensure_parent(remote_copy_local)

        if local_origin.exists():
            local_copy.write_bytes(local_origin.read_bytes())

        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            temp_path = Path(tmp.name)

        try:
            self.connection.sftp.get(remote_origin, str(temp_path))
            remote_copy_local.write_bytes(temp_path.read_bytes())
        finally:
            if temp_path.exists():
                temp_path.unlink()

