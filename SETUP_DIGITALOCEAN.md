# DigitalOcean Setup Guide - Trading Agent

## Step 1: Create a Droplet

1. Go to https://www.digitalocean.com/
2. Sign up (free $200 credit for 60 days for new accounts)
3. Click **"Create" → "Droplets"**
4. Configure:
   - **Image**: Ubuntu 24.04 (LTS) x64
   - **Plan**: Basic → Regular ($6/mo)
     - 1 vCPU
     - 1 GB RAM
     - 25 GB SSD
     - 1000 GB transfer
   - **Datacenter**: Choose closest to you
   - **Authentication**: SSH key (recommended) or password
   - **Hostname**: `trading-bot`
5. Click **"Create Droplet"**
6. Wait ~55 seconds for it to boot
7. Note the **IP address** shown on the dashboard

---

## Step 2: SSH Into Your Droplet

```bash
ssh root@YOUR_DROPLET_IP
```

Enter your password or use your SSH key.

---

## Step 3: Upload Your Code

**Option A — SCP from your local machine:**
```bash
# On your LOCAL machine (not the droplet):
scp -r ~/trading-agent/ root@YOUR_DROPLET_IP:/root/trading-agent
```

**Option B — Git clone (if you push to GitHub):**
```bash
# On the droplet:
git clone https://github.com/YOUR_USERNAME/trading-agent.git ~/trading-agent
```

---

## Step 4: Run Setup

**On the droplet:**
```bash
cd ~/trading-agent
chmod +x deploy-digitalocean.sh
./deploy-digitalocean.sh
```

This installs Docker, builds the image.

---

## Step 5: Authenticate OneDrive (Optional)

```bash
docker compose run --rm trading-agent rclone config create onedrive onedrive
```

Follow the prompts — open the URL in your browser, sign in, paste back the redirect URL.

---

## Step 6: Start the Bot

```bash
docker compose up -d
```

Check it's running:
```bash
docker compose logs -f
```

You should see:
```
AI Trading Agent Started
Strategy: Active (RSI 35/65, MACD, BB, Trend)
```

---

## Managing the Bot

```bash
# View live logs
docker compose logs -f

# View last 50 lines
docker compose logs --tail=50

# Stop the bot
docker compose down

# Restart the bot
docker compose restart

# Update code and restart
cd ~/trading-agent
# (edit files or git pull)
docker compose up -d --build

# Check disk usage
docker system df

# Clean up old images
docker system prune -f
```

---

## Security (Recommended)

```bash
# Create a non-root user
adduser trader
usermod -aG sudo trader
usermod -aG docker trader

# SSH as trader user
ssh trader@YOUR_DROPLET_IP

# Disable root SSH login (optional)
sudo sed -i 's/^PermitRootLogin yes/PermitRootLogin no/' /etc/ssh/sshd_config
sudo systemctl restart sshd
```

---

## Costs

| Item | Monthly Cost |
|---|---|
| Basic Droplet (1GB) | $6.00 |
| Automated Backups | $1.20 |
| **Total** | **$7.20/mo** |

Backups are optional but recommended — they snapshot your droplet daily for 4 weeks.

---

## Monitoring

You can monitor your droplet from the DigitalOcean dashboard:
- CPU, Memory, Disk, Bandwidth graphs
- Alerts (email when CPU > 90% for 5 min, etc.)
- Uptime monitoring (free)

For bot-specific monitoring, check:
```bash
docker compose logs --since 1h | grep -E "BUY|SELL|ERROR|Stop Loss"
```

---

## What Happens If the Droplet Restarts?

The Docker container has `restart: unless-stopped` — it auto-starts on reboot.
The trading agent will resume automatically when the machine comes back up.

---

## Need Help?

Check the logs for errors:
```bash
docker compose logs --tail=100
```

Common issues:
- **API errors**: Check `.env` file has correct keys
- **Out of memory**: Upgrade to $12/mo droplet (2GB RAM)
- **rclone not working**: Re-run `rclone config create onedrive onedrive`
