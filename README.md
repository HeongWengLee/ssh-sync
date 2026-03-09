# ssh-sync

`ssh-sync` is a Python CLI utility for bidirectional file synchronization between a local directory and a remote Ubuntu/Linux server over SSH/SFTP.

It supports pull, push, and two-way sync modes with incremental detection, optional SHA256 verification, resumable transfers, conflict resolution prompts, `.syncignore`, and dry-run planning output.

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
- Recursive local/remote scanning
- Incremental sync using size + mtime
- Optional hash verification (`--use-hash`)
- Conflict resolution menu for bidirectional sync
- SFTP upload/download with resume support
- Rich progress bars for file transfer
- Dry-run mode (`--dry-run`)
- `.syncignore` pattern support
- End-of-run sync summary

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

sync:
  local_dir: ./project
  remote_dir: /home/ubuntu/project

options:
  use_hash: false
  delete_protection: true
```

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

Sample dry-run output:

```
+ upload config.json
+ download data.db
- skip README.md
```

## Ignore Patterns

Create `.syncignore` in your local root:

```
node_modules
*.log
.git
```

## Conflict Resolution

When both local and remote versions changed, interactive options are shown:

1. Use remote version
2. Use local version
3. Keep both versions
4. Show diff
5. Skip

Selecting keep-both creates `.local` and `.remote` variants.

