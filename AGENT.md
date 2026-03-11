# AGENT.md

## 1. Project Overview

`ssh-sync` is a Python CLI application for synchronizing files between a local directory and a remote host over SSH/SFTP. It supports one-way modes (`pull`, `push`) and bidirectional mode (`sync`).

The project’s purpose is to provide a safe, practical sync tool that can:

- compare trees incrementally using metadata and optional hashes,
- plan actions before execution,
- resolve conflicts explicitly,
- transfer files reliably with resume support,
- and preserve operator control with dry-run and policy-based behavior.

Primary entry point: [`sshsync/cli.py`](sshsync/cli.py).

---

## 2. Architecture Design

The implementation follows a modular pipeline where each module owns one responsibility.

### [`cli.py`](sshsync/cli.py)
- Defines Typer commands: `pull`, `push`, `sync`.
- Loads config and applies CLI overrides.
- Constructs runtime services ([`SSHConnection`](sshsync/ssh_connection.py), [`SyncEngine`](sshsync/sync_engine.py)).
- Dispatches selected mode and prints execution summary.

### [`ssh_connection.py`](sshsync/ssh_connection.py)
- Manages SSH and SFTP session lifecycle.
- Supports auth via key/password/agent.
- Applies host-key policy (interactive trust by default).
- Retries transient connection failures with backoff.
- Provides remote command execution utility used by scanner hashing fallback.

### [`scanner.py`](sshsync/scanner.py)
- Scans local and remote trees into normalized relative-path metadata maps.
- Applies `.syncignore` filters consistently.
- Ignores conflict artifacts (`*.local`, `*.remote`).
- Handles symlink behavior (skip by default, optional file-target copy semantics).
- Optionally computes SHA256 (local directly, remote via command fallback).

### [`diff_engine.py`](sshsync/diff_engine.py)
- Pure planning layer.
- Compares metadata and emits a [`SyncPlan`](sshsync/diff_engine.py) containing:
  - upload
  - download
  - delete_local
  - delete_remote
  - conflicts
  - skip
- Implements per-mode planning logic and baseline-aware conflict detection.

### [`transfer.py`](sshsync/transfer.py)
- Implements actual upload/download operations over SFTP.
- Streams file data in chunks.
- Supports resume with prefix-hash validation.
- Integrates progress display.
- Supports optional full-file checksum verification.
- Handles local download temp-file replacement for safer finalization.

### [`sync_engine.py`](sshsync/sync_engine.py)
- Orchestrates end-to-end workflow:
  1. load ignore patterns,
  2. scan both sides,
  3. build plan,
  4. execute plan,
  5. resolve conflicts,
  6. persist sync baseline when safe.
- Tracks counters and results in `SyncResult`.
- Applies deletes and transfer actions, including dry-run behavior.

### [`utils.py`](sshsync/utils.py)
- Shared helpers:
  - logging setup,
  - SHA256 local hashing,
  - relative path normalization,
  - parent-directory creation,
  - ignore-pattern matching.

---

## 3. AI Design Principles

### Separation of concerns
The design intentionally isolates layers (CLI, connection, scan, plan, transfer, orchestration) so each concern can evolve independently.

### Modular architecture
Domain boundaries are explicit:
- metadata model in [`metadata.py`](sshsync/metadata.py),
- deterministic planning in [`diff_engine.py`](sshsync/diff_engine.py),
- side effects in [`transfer.py`](sshsync/transfer.py) and [`sync_engine.py`](sshsync/sync_engine.py).

### Safe synchronization
The system avoids implicit destructive behavior. Deletion requires explicit `--delete`, and baseline updates occur only after successful runs.

### Resumable transfers
Transfers are designed for interrupted networks through offset-based resume with prefix validation.

### Conflict detection
Bidirectional sync uses `last_sync_timestamp` plus metadata change checks to identify true concurrent edits.

### Dry-run safety
Execution and planning are separated so dry-run can print intended actions without mutating either side.

---

## 4. Synchronization Algorithm

Planning is implemented in [`build_pull_plan`](sshsync/diff_engine.py), [`build_push_plan`](sshsync/diff_engine.py), and [`build_sync_plan`](sshsync/diff_engine.py).

### Metadata comparison
Each side is represented as `dict[PurePosixPath, FileMetadata]`. `FileMetadata` includes kind, size, mtime, mode, and optional sha256.

### Size / mtime comparison
Core predicate compares size first, then mtime with tolerance. If size differs, file differs immediately.

### Optional SHA256 hashing
When `use_hash` is enabled and both digests are available, hash is used to refine/confirm difference decisions.

### `last_sync_timestamp` baseline
Bidirectional mode reads [`.sync_state.json`](.sync_state.json) to determine whether local and/or remote changed after last successful sync.

### Conflict detection logic
In bidirectional mode:
- changed on both sides since baseline -> conflict,
- changed only locally -> upload,
- changed only remotely -> download,
- no baseline or ambiguous case -> newer-mtime fallback,
- equal timestamps in fallback path can become conflict/skip depending on context.

---

## 5. Safety Mechanisms

### Dry-run mode
`--dry-run` prints actions (`upload`, `download`, `delete`, `skip`) without writing/removing files.

### Baseline state file (`.sync_state.json`)
Stored in local root and updated only when run succeeds without transfer errors (and without unresolved conflicts in sync mode).

### Interactive host-key verification
Unknown hosts are confirmed interactively and written to known_hosts through custom trust policy.

### Resumable transfers
Partial transfer continuation is guarded by prefix-hash validation to avoid appending to unrelated content.

### Ignore patterns (`.syncignore`)
Pattern-based exclusions are applied during local and remote scanning to prevent accidental sync of ignored paths.

---

## 6. Conflict Resolution Strategy

Conflict handling is executed in [`SyncEngine._resolve_conflict`](sshsync/sync_engine.py).

Supported strategies:
- `interactive`: prompt user per conflict with menu options.
- `remote`: remote version wins (download).
- `local`: local version wins (upload).
- `newer`: compare mtimes and choose newer side; ties become skip.
- `skip`: leave unresolved.

### Keep-both behavior
In interactive mode, choosing “keep both” produces sibling files:
- `<name>.local` from local content,
- `<name>.remote` from remote content (stored locally).

This is implemented in [`SyncEngine._keep_both`](sshsync/sync_engine.py) and preserves both versions for manual reconciliation.

---

## 7. Transfer Engine Design

### Streaming transfers
Upload and download use chunked I/O (1 MiB chunks) to avoid loading whole files into memory.

### Prefix-hash resume validation
Before resuming, the engine compares SHA256 of leading bytes on both sides to validate continuity of partial data.

### Progress bar integration
Each transfer operation uses Rich progress bars for task visibility.

### Planned parallelization
The orchestration layer runs uploads/downloads with a thread pool (`max_workers=4`) for throughput. This is active today and structured so parallelism can be tuned in future revisions.

---

## 8. Future Improvements

The current implementation already includes core versions of several reliability/performance features. Practical next improvements considered are:

1. **Parallel transfers**
   - Make worker count configurable and adaptive to link/host capacity.

2. **Symlink support**
   - Extend beyond current `--copy-links` file-target handling (e.g., explicit symlink replication policy).

3. **Atomic transfers**
   - Extend temp-file + atomic rename strategy to remote uploads where feasible.

4. **Checksum verification**
   - Keep existing `--verify`, and optimize with selective verification or faster remote digest paths.

5. **Connection retry**
   - Expand retry policy beyond initial connect (e.g., transfer-level reconnect/retry strategy).

All items above are framed as enhancements to existing behavior, not new architecture replacements.

---

## 9. How Future AI Agents Should Work on This Repository

1. **Do not rewrite the sync algorithm lightly**
   - Preserve planning semantics in [`diff_engine.py`](sshsync/diff_engine.py), especially baseline-aware conflict logic.

2. **Keep modular structure**
   - Put code in the correct layer:
     - CLI wiring in [`cli.py`](sshsync/cli.py),
     - planning in [`diff_engine.py`](sshsync/diff_engine.py),
     - side effects in [`sync_engine.py`](sshsync/sync_engine.py)/[`transfer.py`](sshsync/transfer.py).

3. **Preserve CLI compatibility**
   - Maintain existing command names/options and behavior contracts unless a migration path is documented.

4. **Maintain dry-run safety**
   - Any new mutating behavior must be gated so `--dry-run` remains side-effect free.

5. **Prefer additive improvements**
   - Introduce enhancements behind clear options and keep defaults conservative.

6. **Reflect actual implementation**
   - Update docs and behavior together; avoid documenting features not present in code.

---

This document is intended as an engineering design record for AI and human contributors working on `ssh-sync`.