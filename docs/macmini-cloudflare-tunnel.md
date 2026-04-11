# Mac mini External Access With Cloudflare Tunnel

This guide exposes the local app running on a Mac mini to the public internet without opening inbound ports on your home network.

Recommended architecture:

```text
Browser -> Cloudflare -> Cloudflare Access -> cloudflared on Mac mini -> http://127.0.0.1:8000
```

Why this is the recommended path:

- The Mac mini only makes outbound connections.
- You do not need to expose your router or forward ports.
- Cloudflare Access can require login before anyone reaches the app.
- Your VPS is not required for the first production setup.

## 1. Move the project to the Mac mini

Copy the whole project directory, including:

- `data/`
- `storage/`
- `.env`

Do not reuse the old `.venv`. Recreate it on the Mac mini.

## 2. Rebuild the Python environment

```bash
cd /path/to/ana
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install -r requirements-openbb.txt
.venv/bin/pip install -r requirements-tushare.txt
.venv/bin/python scripts/init_db.py
```

If you need Qlib support later:

```bash
.venv/bin/pip install -r requirements-qlib.txt
```

## 3. Confirm the app works locally on the Mac mini

Start the app once in the foreground:

```bash
.venv/bin/python -m uvicorn app.api.main:app --host 127.0.0.1 --port 8000
```

Then open:

```text
http://127.0.0.1:8000/dashboard
```

Do not use `--reload` for long-running service mode.

## 4. Install cloudflared

Install `cloudflared` on the Mac mini using the official package or Homebrew:

```bash
brew install cloudflared
```

Then authenticate:

```bash
cloudflared tunnel login
```

This opens a browser and lets you authorize your Cloudflare account and zone.

## 5. Create the tunnel

```bash
cloudflared tunnel create pqw-ana
```

This creates a tunnel ID and credentials file under:

```text
~/.cloudflared/
```

## 6. Create the tunnel config

Use the example file in:

- `deploy/cloudflared/config.example.yml`

Copy it to:

```text
~/.cloudflared/config.yml
```

Then replace:

- `YOUR_TUNNEL_ID`
- `YOUR_DOMAIN`

Recommended hostname:

```text
quant.YOUR_DOMAIN
```

## 7. Route the DNS hostname

Create the DNS route with:

```bash
cloudflared tunnel route dns pqw-ana quant.YOUR_DOMAIN
```

After this, requests to `quant.YOUR_DOMAIN` will be sent to your tunnel.

## 8. Protect the app with Cloudflare Access

In Cloudflare Zero Trust:

1. Go to `Access` -> `Applications`
2. Add a `Self-hosted` application
3. Set the domain to:
   - `quant.YOUR_DOMAIN`
4. Add an allow policy for your email identity

Recommended first policy:

- Allow only your email address or your company domain

This keeps the app private even though it is internet reachable.

## 9. Run the app and tunnel as macOS services

Sample `launchd` files are included:

- `deploy/launchd/com.pqw.ana-app.example.plist`
- `deploy/launchd/com.pqw.cloudflared.example.plist`

The repository intentionally keeps only `.example` templates here.
Create your real local files under `~/.cloudflared/` and `~/Library/LaunchAgents/` instead of committing machine-specific deployment files back into git.

Before loading them:

1. Replace `YOUR_USER`
2. Replace `YOUR_PROJECT_PATH`
3. Replace `YOUR_TUNNEL_ID`

Copy them into:

```text
~/Library/LaunchAgents/
```

Then load:

```bash
launchctl load ~/Library/LaunchAgents/com.pqw.ana-app.plist
launchctl load ~/Library/LaunchAgents/com.pqw.cloudflared.plist
```

To restart after edits:

```bash
launchctl unload ~/Library/LaunchAgents/com.pqw.ana-app.plist
launchctl unload ~/Library/LaunchAgents/com.pqw.cloudflared.plist
launchctl load ~/Library/LaunchAgents/com.pqw.ana-app.plist
launchctl load ~/Library/LaunchAgents/com.pqw.cloudflared.plist
```

## 10. Validate the final setup

Check locally on the Mac mini:

```bash
curl -I http://127.0.0.1:8000/health
cloudflared tunnel info pqw-ana
```

Then test externally:

- `https://quant.YOUR_DOMAIN`

Expected behavior:

- Cloudflare Access login appears first
- After login, the app dashboard opens

## Operational notes

- Keep the app bound to `127.0.0.1`, not `0.0.0.0`
- Keep router port forwarding disabled
- Back up:
  - `.env`
  - `storage/`
  - `data/`
- If the Mac mini sleeps, the tunnel and app become unreachable, so disable sleep for production use

## When to use the VPS instead

Use the VPS only if you later need:

- a second proxy layer
- private site-to-site routing
- multiple internal services behind one custom reverse proxy
- non-Cloudflare ingress

For this app alone, Cloudflare Tunnel is simpler and safer than a VPS reverse tunnel design.
