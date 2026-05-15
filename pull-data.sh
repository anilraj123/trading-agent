#!/bin/bash
# Pull bot data from DigitalOcean droplet to local data/ directory
sshpass -p '[REDACTED]' scp -o StrictHostKeyChecking=no -r root@[REDACTED_IP]:/root/trading-agent/data/ ./data/
echo "Data pulled to ./data/"
ls -la ./data/
