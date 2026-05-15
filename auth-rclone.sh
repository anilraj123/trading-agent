#!/bin/bash
# One-time: authenticate rclone inside the Docker container
# Run this BEFORE starting the bot for the first time

echo "=== Rclone OneDrive Authentication ==="
echo ""
echo "This will guide you through connecting to your OneDrive."
echo "You'll need to open a URL in your browser and paste back the redirect."
echo ""

docker compose run --rm trading-agent rclone config create onedrive onedrive

echo ""
echo "Rclone configured! You can now start the bot:"
echo "  docker compose up -d"
