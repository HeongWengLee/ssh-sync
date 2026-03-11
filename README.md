# ssh-sync

`ssh-sync` is a Python CLI utility for file synchronization between a local directory and a remote Linux host over SSH/SFTP.

It supports `pull`, `push`, and bidirectional `sync`, with incremental planning (size/mtime, optional hash), configurable conflict resolution policies, resumable transfers, optional transfer checksum verification, `.syncignore`, dry-run planning output, and stateful conflict detection based on the last successful sync timestamp.

## Requirements

- Python 3.11+
- `pip` (latest recommended)
- Network access to the target SSH host/port
- One SSH authentication method configured:
  - private key, or
  - password, or
  - SSH agent
- Dependencies from [`requirements.txt`](requirements.txt):
  - `paramiko`
  - `typer`
  - `rich`
  - `pyyaml`

Optional (for remote hash verification with `--use-hash`):

- `sha256sum`, or
- `shasum`, or
- `openssl`

## Features

- SSH connection management with:
  - private key authentication
  - password authentication
  - SSH agent authentication
  - interactive host-key trust prompt for unknown hosts (saved to `~/.ssh/known_hosts`)
  - retry with exponential backoff for transient SSH/network failures (2s, 4s, 8s)
- Recursive local/remote scanning over filesystem + SFTP
- Incremental comparison using size + mtime (with optional SHA256 verification)
- Optional hash verification (`--use-hash`)
- Conflict resolution menu for bidirectional sync and policy-driven auto-resolution (`--conflict-policy`)
- Text diff preview for conflicts (`Show diff`), with binary-file detection
- SFTP upload/download with resume support
- Optional end-to-end transfer validation (`--verify`) with SHA256 on both sides
- File permission synchronization for uploaded/downloaded files
- Optional symlink following (`--copy-links`) to copy linked file contents
- Parallel upload/download execution (up to 4 workers)
- Remote parent directory auto-creation on upload
- Dry-run mode (`--dry-run`) that prints planned actions
- Deletion mode (`--delete`) to remove one-sided files
- `.syncignore` pattern support (applied to both local and remote relative paths)
- Automatic ignoring of conflict artifact files (`*.local`, `*.remote`)
- Baseline safety state file (`.sync_state.json`) for conflict detection
- End-of-run sync summary

## Architecture Overview

- [`sshsync/cli.py`](sshsync/cli.py): Typer commands and option parsing (`pull`, `push`, `sync`)
- [`sshsync/ssh_connection.py`](sshsync/ssh_connection.py): SSH/SFTP lifecycle and remote command execution
- [`sshsync/scanner.py`](sshsync/scanner.py): local/remote tree scanning, ignore filtering, optional hashing
- [`sshsync/diff_engine.py`](sshsync/diff_engine.py): transfer/delete/conflict planning algorithm
- [`sshsync/transfer.py`](sshsync/transfer.py): resumable upload/download with progress bars
- [`sshsync/sync_engine.py`](sshsync/sync_engine.py): orchestration, conflict workflow, stats, last-sync state

High-level flow:

1. Load config + CLI overrides
2. Connect SSH/SFTP
3. Load ignore patterns from `.syncignore`
4. Scan local + remote trees
5. Build plan (`upload`, `download`, `delete`, `skip`, `conflicts`)
6. Execute plan in parallel transfers (or print only in dry-run)
7. Resolve conflicts interactively in `sync` mode
8. Print summary and persist last successful sync timestamp (when eligible)

## Project Structure

```
ssh-sync/
├── sshsync/
│   ├── __init__.py
│   ├── cli.py
│   ├── config.py
│   ├── ssh_connection.py
│   ├── metadata.py
│   ├── scanner.py
│   ├── diff_engine.py
│   ├── transfer.py
│   ├── sync_engine.py
│   └── utils.py
├── config.yaml.example
├── README.md
├── requirements.txt
└── install.sh
```

## Installation

### Linux / macOS

```bash
chmod +x install.sh
./install.sh
```

This installs dependencies and creates an executable command:

```bash
ssh-sync
```

### Windows

Install dependencies:

```powershell
py -m pip install -r requirements.txt
```

Run via module:

```powershell
py -m sshsync.cli --help
```

## SSH Key Setup Guide

Generate a key pair:

```bash
ssh-keygen -t ed25519
```

Copy public key to remote server:

```bash
ssh-copy-id ubuntu@ubuntu-server.example.com
```

Test login:

```bash
ssh ubuntu@ubuntu-server.example.com
```

## Configuration

Copy `config.yaml.example` to `config.yaml` and edit values.

Example:

```yaml
ssh:
  host: ubuntu-server.example.com
  port: 22
  user: ubuntu
  private_key: ~/.ssh/id_rsa
  # password: optional
  # use_agent: true

sync:
  local_dir: ./project
  remote_dir: /home/ubuntu/project

options:
  use_hash: false
  delete_protection: true
```

Notes:

- CLI flags override config values.
- `delete_protection` is currently parsed but not enforced by runtime planning; deletion behavior is controlled by `--delete`.

## Usage

### Pull files from server

```bash
ssh-sync pull \
  --remote-host ubuntu-server.example.com \
  --remote-dir /home/user/project \
  --local-dir ./project
```

### Push files to server

```bash
ssh-sync push \
  --local-dir ./project \
  --remote-host ubuntu-server.example.com \
  --remote-dir /home/user/project
```

### Bidirectional sync

```bash
ssh-sync sync \
  --remote-host ubuntu-server.example.com \
  --remote-dir /home/user/project \
  --local-dir ./project
```

### Use config file

```bash
ssh-sync sync --config ./config.yaml
```

### Dry-run mode

```bash
ssh-sync sync --dry-run --config ./config.yaml
```

### Example CLI usage (common options)

```bash
ssh-sync sync \
  --config ./config.yaml \
  --remote-host ubuntu-server.example.com \
  --remote-port 22 \
  --remote-user ubuntu \
  --private-key ~/.ssh/id_ed25519 \
  --use-agent \
  --local-dir ./project \
  --remote-dir /home/ubuntu/project \
  --use-hash \
  --delete \
  --dry-run \
  --verbose
```

Available command options (`pull`, `push`, `sync`):

- `--config`: YAML config path
- `--remote-host`: SSH host
- `--remote-port`: SSH port (default: 22)
- `--remote-user`: SSH username
- `--remote-password`: SSH password
- `--private-key`: private key path
- `--use-agent / --no-agent`: enable/disable SSH agent + key discovery
- `--local-dir`: local root directory
- `--remote-dir`: remote root directory
- `--use-hash`: include SHA256 in comparison logic
- `--delete`: remove files that exist on only one side
- `--copy-links`: follow symlinks and copy target file content (default: skip symlinks)
- `--conflict-policy`: `interactive` (default), `remote`, `local`, `newer`, `skip`
- `--verify`: after each transfer, compare full SHA256 across local/remote and fail on mismatch
- `--dry-run`: show actions without transferring/deleting
- `--verbose`: debug logging

Sample dry-run output:

```
+ upload config.json
+ download data.db
- delete old.log
- skip README.md
Ignored/skipped by scanner: 4
```

## Sync Algorithm

Planning is implemented in `diff_engine` and uses per-file metadata from both sides.

### Comparison rules

1. Different size => different file
2. Same size but different mtime => different file
3. With `--use-hash`, differing SHA256 also marks file different

### Pull mode (`pull`)

- Remote-only file => download (or delete remote when `--delete` is enabled)
- Local+remote and different => download
- Local-only file => kept by default; deleted locally with `--delete`

### Push mode (`push`)

- Local-only file => upload (or delete local when `--delete` is enabled)
- Local+remote and different => upload
- Remote-only file => kept by default; deleted remotely with `--delete`

### Bidirectional mode (`sync`)

- One-sided files: upload/download by default, or delete that side with `--delete`
- Identical files: skip
- Different files:
  - If baseline timestamp exists (`.sync_state.json`), detect whether each side changed since last successful sync:
    - both changed => conflict
    - only local changed => upload
    - only remote changed => download
    - neither changed since baseline but still different => choose newer mtime
  - If no baseline exists => choose newer mtime; if mtimes are effectively equal, mark conflict

## SSH Connection Behavior

- Loads system host keys first.
- For unknown hosts, prompts interactively to trust and continue.
- Accepted unknown host keys are persisted to `~/.ssh/known_hosts` when possible.
- Auth parameters used by Paramiko:
  - `pkey` from `--private-key` (if supplied)
  - `password` from `--remote-password` (if supplied)
  - agent/key discovery controlled by `--use-agent/--no-agent`
- Retries transient connection failures up to 3 times with exponential backoff (2s, 4s, 8s), covering `paramiko.SSHException` and socket-level errors.
- Opens one SSH client and one SFTP session per run via context manager, then reuses that session for scanning, planning, and all transfers.
- Runtime supports an `insecure` mode internally (auto-accept host keys), but CLI does not currently expose an `--insecure` flag.

## Scanner Behavior

- Local scanner walks recursively with `os.walk`.
- Remote scanner walks recursively with `SFTP.listdir_attr`.
- By default symlinks are skipped on both sides (warning logged).
- With `--copy-links`, symlinks to regular files are followed and treated as normal files by the scanner (target metadata/content is used).
- Symlinks to directories remain skipped to preserve planner behavior and avoid traversal ambiguity.
- `.syncignore` is read only from local root and then applied to both local and remote relative paths.
- Ignore lines:
  - blank lines and `#` comments are ignored
  - patterns use `fnmatch` against full relative path and filename
  - trailing `/` pattern acts as directory-prefix ignore
- Conflict artifact files ending in `.local` or `.remote` are always ignored by scanner.

## Resumable Transfers

Implemented in `transfer.py` for both upload and download:

- Transfers use 1 MiB streaming chunks with Rich progress bars.
- Resume decisions use existing target size and a SHA256 of the first 64 KiB (prefix hash) from both sides.
- If prefix hash mismatches, transfer restarts from byte `0`.
- Upload resume is additionally guarded by mtime: if local file is newer than remote file, resume is disabled and file is rewritten.
- Parent directories are created automatically:
  - local parent on download
  - remote parent path on upload
- Permissions are synchronized after transfer:
  - upload: applies local mode to remote file via SFTP `chmod`
  - download: applies remote mode to local file via `chmod`
- With `--verify`, each completed transfer is validated with a full SHA256 comparison between local and remote copies.

## Parallel Transfer Design

- Upload and download phases are executed with a thread pool (`max_workers=4`).
- Dry-run behavior is unchanged: actions are printed, no file writes occur.
- Existing single `SSHConnection` lifecycle is preserved: one SSH client + one SFTP session are reused across operations.
- Progress display remains per-file and is still shown during active transfers.

## Ignore Patterns

Create `.syncignore` in your local root:

```
node_modules
*.log
.git/
build/
```

Behavior summary:

- Pattern matching applies to relative paths from sync roots.
- A filename-only pattern (for example `*.log`) matches anywhere.
- Directory-style patterns with trailing `/` ignore matching prefixes.

## Conflict Resolution

When both local and remote versions changed (or mtimes tie without baseline), resolution supports both policy mode and interactive mode.

`--conflict-policy` values:

- `interactive` (default): show menu and ask per conflict
- `remote`: always choose remote version (download)
- `local`: always choose local version (upload)
- `newer`: choose by newer mtime; if mtimes are equal, skip
- `skip`: skip all conflicts

In interactive mode, options are:

1. Use remote version (download)
2. Use local version (upload)
3. Keep both versions
4. Show diff
5. Skip

Details:

- `Show diff` prints a unified text diff (`local/...` vs `remote/...`).
- Binary files are detected by null-byte probe and do not show textual diff.
- `Keep both versions` writes sibling files such as `name.local` and `name.remote` in the local tree.
- Choosing `Skip` leaves conflict unresolved for this run.

## Delete Support (`--delete`)

`--delete` changes one-sided file behavior from “copy missing file” to “delete the one-sided file”.

- In all modes, the planner can schedule both local and remote deletions for files missing on the opposite side.
- In `sync` mode, this effectively converges toward intersection of existing paths (except conflicts/resolution outcomes).
- In `dry-run`, planned deletions are printed as `- delete <path>`.

Use with caution because it is intentionally destructive.

## Symlink Support (`--copy-links`)

- Default behavior: symlinks are skipped during scan.
- With `--copy-links` enabled:
  - symlinks to regular files are followed
  - transfer copies file bytes, not link objects
  - directory symlinks remain skipped

## Statistics Output

Each command prints a final summary containing:

- `Total scanned`
- `Ignored`
- `Uploaded`
- `Downloaded`
- `Conflicts`
- `Duration`

The engine also prints `Ignored/skipped by scanner: <count>` during execution.

Example final output:

```
Sync Summary
------------
Total scanned:    142
Ignored:          11
Uploaded:         5
Downloaded:       3
Conflicts:        1
Duration:         2.184s
```

## Safety Guarantees

The sync workflow includes the following safety mechanisms:

- **Host trust prompt by default**: unknown SSH hosts require explicit user confirmation.
- **Dry-run planning**: inspect all planned uploads/downloads/deletions before applying.
- **Conflict gating**: potential concurrent edits are surfaced interactively instead of auto-overwriting.
- **Baseline timestamp (`.sync_state.json`)**:
  - Used in `sync` mode to determine if local and/or remote changed since last successful run.
  - Updated only when all of the following are true:
    - not `--dry-run`
    - no transfer/delete errors occurred
    - no conflict was skipped unresolved

This prevents advancing the baseline after partial or ambiguous sync runs.

