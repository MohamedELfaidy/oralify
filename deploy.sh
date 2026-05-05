#!/bin/bash
# Oralify Deployment Script
# Run this script from the server to pull the latest changes and restart the app.

PROJECT_DIR="/var/www/oralify"

echo "======================================"
echo " Starting Oralify Deployment Update..."
echo "======================================"

# 1. Navigate to the project directory
cd "$PROJECT_DIR" || { echo "Directory $PROJECT_DIR not found. Exiting."; exit 1; }

# 2. Pull the latest code from GitHub
echo "--> Pulling latest changes from GitHub..."
# Note: Ensure that the 'oralify' user has the correct git permissions if it's a private repo.
# Since it's public, this will work seamlessly.
git pull origin main

# 3. Update Python dependencies in the virtual environment
echo "--> Updating Python dependencies..."
source venv/bin/activate
pip install -r requirements.txt
deactivate
# 4. Restart the Gunicorn server via systemd
echo "--> Restarting the oralify service..."
# Note: The user running this script must have sudo privileges to restart the service.
sudo systemctl restart oralify

echo "======================================"
echo " Deployment Complete! App is live. "
echo "======================================"
