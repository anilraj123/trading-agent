#!/bin/bash
# Cloud migration helper for trading-agent
# Run this on the new cloud VM after copying the trading-agent/ directory

set -e

echo "=== Trading Agent Cloud Setup ==="
echo ""

# Check prerequisites
echo "Checking prerequisites..."
if ! command -v docker &>/dev/null; then
    echo "Docker not found. Installing..."
    curl -fsSL https://get.docker.com | sh
    sudo usermod -aG docker $USER
    echo "Docker installed. You may need to log out and back in."
fi

if ! command -v docker compose &>/dev/null; then
    echo "Docker Compose not found. Installing..."
    sudo apt-get install -y docker-compose-plugin 2>/dev/null || \
    curl -SL https://github.com/docker/compose/releases/latest/download/docker-compose-$(uname -s)-$(uname -m) \
        -o /usr/local/bin/docker-compose && chmod +x /usr/local/bin/docker-compose
fi

echo "Docker version: $(docker --version)"
echo "Compose version: $(docker compose version 2>/dev/null || echo 'checking docker-compose')"
echo ""

# Build and run
echo "Building trading-agent image..."
docker compose build

echo ""
echo "=== Setup complete ==="
echo ""
echo "Next steps:"
echo ""
echo "1. Authenticate rclone for OneDrive backup:"
echo "   docker compose exec trading-agent rclone config create onedrive onedrive"
echo "   (Follow the interactive prompts)"
echo ""
echo "2. Start the bot:"
echo "   docker compose up -d"
echo ""
echo "3. View logs:"
echo "   docker compose logs -f"
echo ""
echo "4. Stop the bot:"
echo "   docker compose down"
echo ""
echo "5. Update (rebuild + restart):"
echo "   docker compose up -d --build"
