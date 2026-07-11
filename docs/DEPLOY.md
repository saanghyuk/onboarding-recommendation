# Deployment Guide

**Purpose**: get this service running in production with persistent state, TLS, and a weekly-batch trigger.

---

## 0. Constraints

- The FastAPI process is **stateful** — SQLite (`data/bandit.db`) and the lookup JSON must survive restarts.
- Pure serverless (Vercel Functions, Netlify Functions) is not suitable for the API. You can, however, host the static UIs on Vercel and point them at an API running elsewhere.
- You need a persistent volume mounted at `data/` (or `DATA_DIR`).

---

## 1. Option matrix

| Option | Cost / mo | Time to first prod | Scaling | Recommendation |
|---|---|---|---|---|
| **Docker + any VPS** | ~$5–15 | 30 min | Manual | Simplest reproducible path |
| **Fly.io** | ~$5 | 30 min | Regional | Great for MVP |
| **Railway** | ~$5–10 | 15 min | Autoscale | Fastest wire-up |
| **Render** | ~$7 | 30 min | Autoscale | Solid alternative |
| **AWS EC2 t3.small** | ~$15 | 2 h | Vertical | Full control |
| **AWS ECS Fargate** | ~$20+ | 4 h+ | Horizontal | Over-engineered for MVP |
| **Vercel (frontend only)** | Free–$20 | 10 min | N/A | Serve `static/` only; API elsewhere |

---

## 2. Docker (universal)

### 2.1 `Dockerfile`

```dockerfile
FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py ./
COPY scripts ./scripts
COPY static ./static

# Persistent state mount point
ENV DATA_DIR=/data
RUN mkdir -p /data

EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
```

### 2.2 Build & run
```bash
docker build -t onboarding-recommendation:latest .
docker run -d --name reco \
  -p 8000:8000 \
  -v $(pwd)/data:/data \
  -e ADMIN_TOKEN=$(openssl rand -hex 16) \
  -e ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY \
  onboarding-recommendation:latest
```

### 2.3 `docker-compose.yml` snippet
```yaml
services:
  reco:
    build: .
    ports: ["8000:8000"]
    volumes: ["./data:/data"]
    env_file: .env
    restart: unless-stopped
```

---

## 3. Fly.io

### 3.1 `fly.toml`
```toml
app = "onboarding-recommendation"
primary_region = "nrt"

[build]

[[mounts]]
  source = "reco_data"
  destination = "/data"

[env]
  DATA_DIR = "/data"

[[services]]
  internal_port = 8000
  protocol = "tcp"

  [services.concurrency]
    hard_limit = 50
    soft_limit = 25

  [[services.ports]]
    port = 80
    handlers = ["http"]

  [[services.ports]]
    port = 443
    handlers = ["tls", "http"]
```

### 3.2 Deploy
```bash
flyctl volumes create reco_data --region nrt --size 1
flyctl secrets set ADMIN_TOKEN=$(openssl rand -hex 16) ANTHROPIC_API_KEY=sk-...
flyctl deploy
flyctl status
```

### 3.3 Weekly cron
Fly does not run cron. Use GitHub Actions to hit `/api/rebuild`:
```yaml
# .github/workflows/weekly-build.yml
on:
  schedule:
    - cron: '0 19 * * 0'   # Monday 04:00 KST
jobs:
  rebuild:
    runs-on: ubuntu-latest
    steps:
      - run: |
          curl -X POST https://onboarding-recommendation.fly.dev/api/rebuild \
            -H "X-Admin-Token: ${{ secrets.ADMIN_TOKEN }}"
```

### 3.4 Uploading weekly CSVs
```bash
flyctl ssh sftp shell
put data/incoming/*.csv /data/incoming/
put data/incoming/*.parquet /data/incoming/
```

---

## 4. Railway

1. Sign in with GitHub → New Project → Deploy from repo.
2. Root directory: `service_app/`.
3. Attach a 1 GB volume mounted at `/data`.
4. `railway.toml` (optional):
```toml
[build]
  builder = "nixpacks"

[deploy]
  startCommand = "uvicorn app:app --host 0.0.0.0 --port $PORT"
  healthcheckPath = "/api/health"
  healthcheckTimeout = 10
```
5. Trigger the weekly rebuild via GitHub Actions or cron-job.org calling `/api/rebuild`.

---

## 5. AWS EC2 t3.small

### 5.1 Provision
- Ubuntu 22.04 LTS, 20 GB EBS, security group open on 80/443/22 (office IP only).

### 5.2 Setup
```bash
sudo apt update
sudo apt install -y python3.13 python3.13-venv git nginx certbot python3-certbot-nginx
git clone <repo> /app && # already at repo root
python3.13 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 5.3 systemd (`/etc/systemd/system/reco.service`)
```
[Unit]
Description=Onboarding Recommendation Reco FastAPI
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/app/service_app
EnvironmentFile=/app/service_app/.env
ExecStart=/app/service_app/.venv/bin/uvicorn app:app --host 0.0.0.0 --port 8000
Restart=always

[Install]
WantedBy=multi-user.target
```
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now reco.service
```

### 5.4 Nginx reverse proxy + TLS
```nginx
server {
    listen 80;
    server_name reco.example.com;
    return 301 https://$server_name$request_uri;
}
server {
    listen 443 ssl http2;
    server_name reco.example.com;
    ssl_certificate     /etc/letsencrypt/live/reco.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/reco.example.com/privkey.pem;

    limit_req_zone $binary_remote_addr zone=reco:10m rate=60r/m;

    location / {
        limit_req zone=reco burst=20 nodelay;
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```
```bash
sudo certbot --nginx -d reco.example.com
```

### 5.5 Weekly cron
```
0 4 * * 1 cd /app/service_app && /app/service_app/.venv/bin/python scripts/service_reco_weekly_build.py >> /var/log/reco_build.log 2>&1
*/5 * * * * /app/service_app/scripts/check_and_build.sh >> /var/log/reco_watcher.log 2>&1
```

### 5.6 Uploading weekly CSVs
Simplest: `scp` from the analyst's laptop after the notebook finishes.
```bash
scp data/incoming/*.csv data/incoming/*.parquet ubuntu@reco.example.com:/data/incoming/
```

---

## 6. Vercel (frontend only)

If you want to host the demo UIs (`static/`) on Vercel while the API runs on Fly / Railway / EC2:

- Set the Vercel project's root directory to `service_app/static/`.
- Add a `vercel.json` that rewrites `/api/*` to your API host:
```json
{
  "rewrites": [
    {"source": "/api/(.*)", "destination": "https://reco.example.com/api/$1"}
  ]
}
```
- Set CORS on the API to allow your Vercel domain.

---

## 7. Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `DATA_DIR` | yes (Docker) | Persistent volume mount path |
| `ADMIN_TOKEN` | yes | Guards `POST /api/rebuild` (see `app.py:485` for the env-var name in your build) |
| `ANTHROPIC_API_KEY` | no | Enables the LLM path in `/api/nlu` |
| `SLACK_WEBHOOK` | no | Weekly build success / failure alerts |
| `SENTRY_DSN` | no | Prod error tracing |

---

## 8. Hand-off checklist

- [ ] Server deployed on chosen platform
- [ ] `data/` persisted (volume / EBS)
- [ ] `/api/health` returns 200
- [ ] TLS certificate valid, HTTP → HTTPS redirect in place
- [ ] CORS restricted to production domains
- [ ] Rate limiting active
- [ ] Weekly rebuild trigger scheduled (cron / Actions / cron-job.org)
- [ ] Upload path decided (SCP, SFTP, S3, git commit)
- [ ] Alerts wired (Slack, Sentry)
- [ ] First lookup generated, real recommendations returned
- [ ] Frontend team integrated, first realtime feedback appears in `bandit-stats`

---

## Related Files

- [SETUP.md](SETUP.md) — local dev
- [WEEKLY_WORKFLOW.md](WEEKLY_WORKFLOW.md) — weekly human step
- [FRONTEND_INTEGRATION.md](FRONTEND_INTEGRATION.md) — client integration
- [PRODUCTION_ROADMAP.md](PRODUCTION_ROADMAP.md) — what still needs to be done to be production-ready
