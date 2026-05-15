#!/bin/bash
# =============================================================================
# DigitalOcean Trading Agent Setup
# =============================================================================
# This script is for the cloud VM. Run it after creating a droplet.
# =============================================================================

set -e

echo "============================================"
echo "  Trading Agent - DigitalOcean Setup"
echo "============================================"
echo ""

# Step 1: Install Docker
echo "[1/5] Installing Docker..."
if command -v docker &>/dev/null; then
    echo "  Docker already installed: $(docker --version)"
else
    curl -fsSL https://get.docker.com | sh
    sudo usermod -aG docker $USER
    echo "  Docker installed: $(docker --version)"
fi
echo ""

# Step 2: Create trading-agent directory
echo "[2/5] Setting up trading-agent directory..."
mkdir -p ~/trading-agent
cp .env docker-compose.yml Dockerfile .dockerignore ~/trading-agent/ 2>/dev/null || true
cp -r trader/ ~/trading-agent/ 2>/dev/null || true
echo "  Done"
echo ""

# Step 3: Build Docker image
echo "[3/5] Building Docker image..."
cd ~/trading-agent
docker compose build
echo ""

# Step 4: Rclone auth for OneDrive
echo "[4/5] Rclone OneDrive setup..."
echo "  Run: docker compose exec trading-agent rclone config create onedrive onedrive"
echo "  (Follow prompts to connect your OneDrive for backups)"
echo ""

# Step 5: Start the bot
echo "[5/5] To start the bot:"
echo "  cd ~/trading-agent"
echo "  docker compose up -d"
echo ""
echo "  View logs:  docker compose logs -f"
echo "  Stop:       docker compose down"
echo "  Update:     git pull && docker compose up -d --build"
echo ""
echo "============================================"
echo "  Setup complete!"
echo "============================================"
