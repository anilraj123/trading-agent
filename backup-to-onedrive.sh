#!/bin/bash
# Backup trading-agent to OneDrive via rclone
# Usage: ./backup-to-onedrive.sh

SOURCE="/home/[USER]/trading-agent"
DEST="onedrive:trading-agent-backup"
RCLONE="/usr/bin/rclone"

echo "=== OneDrive Backup ==="
echo "Source: $SOURCE"
echo "Dest:   $DEST"
echo ""

# Create the folder on OneDrive if it doesn't exist
$RCLONE mkdir "$DEST" 2>/dev/null

# Sync: upload new/changed files, delete nothing on source
$RCLONE sync "$SOURCE" "$DEST" \
    --exclude "venv/**" \
    --exclude "__pycache__/**" \
    --exclude "*.pyc" \
    --exclude ".git/**" \
    --exclude "backtest_*.py" \
    --exclude "trading-agent-backup.log" \
    -v \
    --log-file="/home/[USER]/trading-agent/trading-agent-backup.log"

echo ""
echo "Backup complete: $(date)"
echo "Log: /home/[USER]/trading-agent/trading-agent-backup.log"
