# Oralify – VPS Deployment Guide
**Server IP:** 35.207.93.121  
**Domain:** oralify.dpdns.org (+ www.oralify.dpdns.org)

---

## Step 1 — Point Your Domain to the Server

Log in to your DNS provider for `dpdns.org` and add these two records:

| Type | Name | Value | TTL |
|------|------|-------|-----|
| A | `oralify` | `35.207.93.121` | 300 |
| A | `www.oralify` | `35.207.93.121` | 300 |

Wait a few minutes for DNS to propagate before continuing.

---

## Step 2 — SSH into the Server

```bash
ssh root@35.207.93.121
```

---

## Step 3 — Create a Dedicated User for Oralify

```bash
# Create the user (no login shell needed for the app, but we need sudo for setup)
adduser oralify

# Give it sudo so we can run system commands during setup
usermod -aG sudo oralify

# Switch to the new user
su - oralify
```

---

## Step 4 — Install System Dependencies

```bash
sudo apt update && sudo apt upgrade -y

sudo apt install -y \
  python3 python3-pip python3-venv \
  nginx certbot python3-certbot-nginx \
  git curl build-essential libgl1
```

> `libgl1` is required by EasyOCR for image processing.

---

## Step 5 — Upload the Project

**From your local machine** (run this on your laptop, not the server):

```bash
scp -r /path/to/oralify oralify@35.207.93.121:/home/oralify/
```

Or if you have it in a git repo:

```bash
# On the server, as user oralify:
cd ~
git clone https://your-repo-url/oralify.git oralify
```

---

## Step 6 — Set Up Python Virtual Environment

```bash
cd ~/oralify
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
pip install gunicorn
```

> EasyOCR will download its model (~100 MB) on first use — this is normal.

---

## Step 7 — Create the .env File

```bash
nano ~/oralify/.env
```

Paste this (replace the values):

```
GROQ_API_KEY=your_groq_api_key_here
SECRET_KEY=some-long-random-string-here
```

Save with `Ctrl+X → Y → Enter`.

Lock down the permissions:

```bash
chmod 600 ~/oralify/.env
```

---

## Step 8 — Test the App Runs

```bash
cd ~/oralify
source venv/bin/activate
flask run --host=0.0.0.0 --port=5000
```

Visit `http://35.207.93.121:5000` — if you see Oralify, it works.  
Stop it with `Ctrl+C`.

---

## Step 9 — Create a systemd Service

This keeps the app running after logout and restarts it on crash.

```bash
sudo nano /etc/systemd/system/oralify.service
```

Paste exactly:

```ini
[Unit]
Description=Oralify Oral Exam Assistant
After=network.target

[Service]
User=oralify
Group=oralify
WorkingDirectory=/home/oralify/oralify
Environment="PATH=/home/oralify/oralify/venv/bin"
EnvironmentFile=/home/oralify/oralify/.env
ExecStart=/home/oralify/oralify/venv/bin/gunicorn \
    --workers 2 \
    --bind 127.0.0.1:5000 \
    --timeout 120 \
    app:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Save and enable the service:

```bash
sudo systemctl daemon-reload
sudo systemctl enable oralify
sudo systemctl start oralify

# Check it is running
sudo systemctl status oralify
```

You should see `Active: active (running)`.

---

## Step 10 — Configure Nginx

```bash
sudo nano /etc/nginx/sites-available/oralify
```

Paste:

```nginx
server {
    listen 80;
    server_name oralify.dpdns.org www.oralify.dpdns.org;

    # Increase upload size limit to 50 MB (matches Flask config)
    client_max_body_size 50M;

    location / {
        proxy_pass         http://127.0.0.1:5000;
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
        proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;
        proxy_connect_timeout 10s;
    }

    location /static/ {
        alias /home/oralify/oralify/static/;
        expires 30d;
        add_header Cache-Control "public, immutable";
    }
}
```

Enable the site:

```bash
sudo ln -s /etc/nginx/sites-available/oralify /etc/nginx/sites-enabled/

# Test config syntax
sudo nginx -t

# Reload
sudo systemctl reload nginx
```

---

## Step 11 — Get HTTPS Certificate (Let's Encrypt)

```bash
sudo certbot --nginx -d oralify.dpdns.org -d www.oralify.dpdns.org
```

Follow the prompts:
- Enter your email
- Agree to terms
- Choose **2 (Redirect)** when asked about HTTP → HTTPS redirect

Certbot will automatically update your Nginx config to use SSL.

Test auto-renewal:

```bash
sudo certbot renew --dry-run
```

---

## Step 12 — Fix Folder Permissions

The `oralify` user needs write access to create the SQLite database:

```bash
# The instance folder is where Flask stores exam.db
mkdir -p /home/oralify/oralify/instance
chmod 755 /home/oralify/oralify/instance
chown oralify:oralify /home/oralify/oralify/instance

# The user's home must be readable by nginx
chmod 755 /home/oralify
```

---

## Step 13 — Final Test

Visit these URLs — both should load Oralify over HTTPS:

- https://oralify.dpdns.org
- https://www.oralify.dpdns.org

---

## Useful Commands After Deployment

```bash
# View live logs
sudo journalctl -u oralify -f

# Restart after code changes
sudo systemctl restart oralify

# Check nginx errors
sudo tail -f /var/log/nginx/error.log

# Check app is listening
sudo ss -tlnp | grep 5000
```

---

## Updating the App

```bash
# As user oralify
cd ~/oralify

# Upload new files from your laptop:
# scp -r /path/to/oralify/* oralify@35.207.93.121:/home/oralify/oralify/

# Then restart the service
sudo systemctl restart oralify
```

---

## Removing sudo from oralify (after setup is complete)

Once everything is deployed, you can remove sudo access from the app user for security:

```bash
sudo deluser oralify sudo
```

The systemd service runs as `oralify` but does not need sudo to operate.