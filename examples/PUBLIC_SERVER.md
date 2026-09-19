# Public demo — timetable.shinick.dev

URL: https://timetable.shinick.dev/#/live

The Cloudflare account owns the named tunnel `scheduler-harness-demo`
(`7dc7e040-792d-42f8-8bb3-8236b3a12e54`) and the DNS route for this hostname.
The former trycloudflare.com Quick Tunnel was removed upstream and is no longer used.
Existing sms/coach/toolshed tunnel configuration was not changed.

## What runs where

- Cloudflare: DNS, HTTPS public entry, named tunnel routing.
- This PC: Python web server on 127.0.0.1:5191 and cloudflared connector.
- Daytona: actual dataset loading and schedule planning execution.

A named tunnel keeps the hostname stable; it does not move the Python app into
Cloudflare Workers or keep this PC awake. The PC, app and connector must stay on.
No automatic boot task or Windows service was installed. A remote always-on app
deployment is separate work.

## Restart on this PC

From the repository root, start the app in one terminal:

```powershell
$env:HARNESS_PUBLIC_ORIGIN = 'https://timetable.shinick.dev'
python web_demo.py
```

Or use `실행.bat`, which now supplies that origin by default.
The server needs DAYTONA_API_KEY in its process environment.

In another terminal, reuse the saved named-tunnel config:

```powershell
cloudflared tunnel --config .runtime/named-tunnel.yml --no-autoupdate run 7dc7e040-792d-42f8-8bb3-8236b3a12e54
```

On this PC `.runtime/named-tunnel.yml` refers to the credential JSON in the user's
`.cloudflared` directory. Credentials and runtime config are intentionally not committed.
Always pass this config explicitly: the user's default config belongs to another service.
Do not create a new tunnel or overwrite DNS when restarting.

Verified at setup: public page HTTP 200, public recommendation POST HTTP 200,
runtime_provider=daytona, execution_ok=true, 600 jobs processed; named connector
registered four edge connections. This is a point-in-time check, not an uptime guarantee.
