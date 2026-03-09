#!/usr/bin/env bash
set -euo pipefail

python3 -m pip install -r requirements.txt

TARGET_BIN="${HOME}/.local/bin"
mkdir -p "${TARGET_BIN}"

cat > "${TARGET_BIN}/ssh-sync" <<'EOF'
#!/usr/bin/env bash
python3 -m sshsync.cli "$@"
EOF

chmod +x "${TARGET_BIN}/ssh-sync"

echo "Installed ssh-sync to ${TARGET_BIN}/ssh-sync"
echo "Ensure ${TARGET_BIN} is in your PATH"

