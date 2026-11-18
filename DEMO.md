# Vytal live demo

Blood donation management system (COMP-4150). Flask GUI on PostgreSQL 16. All data is made up.

## Open it
1. On GitHub press **Code → Codespaces → Create codespace on main**.
2. Wait about 3 minutes for the first build. The app opens by itself on port 5050.
3. To share the link: open the **Ports** tab, right-click port 5050, set **Port visibility → Public**, and copy the URL.

The Codespace stops after inactivity, so the link only works while it is running. Reopen the Codespace to bring it back.

## Sign in
The sign-in and sign-up pages list every account. All of them use the password `Vytal-demo-2026`.

| Role | Email | Sees |
|---|---|---|
| Admin | admin@vytal.example.ca | accounts, facilities, clinical rules, audit log, lookback |
| Staff | m.ross@riverside.example.ca | stock, requests, donors, tests (Riverside General) |
| Donor | liam.tremblay@mail.ca | eligibility, history, calls, screening |

## Run it on your own machine
```
createdb vytal && ./setup.sh
VYTAL_DEMO=1 PGHOST=localhost ./run_gui.sh     # http://127.0.0.1:5050
```

## Hosted demo (VPS with Docker)
The public demo runs the same app in one container (`Dockerfile`, `docker/entrypoint.sh`): PostgreSQL 16 and the GUI, with the sample data built fresh on every start.

```
./deploy-vps.sh user@HOST              # build and run the container (plain HTTP on port 80 at first)
sudo bash enable-https.sh DOMAIN       # on the VPS: Caddy with Let's Encrypt in front, app moved to 127.0.0.1:7860
./deploy-vps.sh user@HOST              # later updates: keeps the HTTPS setup if Caddy is already in front
```

- **Daily reseed.** `deploy-vps.sh` installs a systemd timer (`vytal-reseed.timer`) that restarts the `vytal` container every day at 04:00 Toronto time. A restart rebuilds the sample data at that day's date, so open requests are due in the future again and no expired unit sits on the shelf. Check it with `systemctl list-timers vytal-reseed.timer`; reseed by hand with `sudo docker restart vytal` (about 30 s). Hosts without timezone-aware systemd timers get `/etc/cron.d/vytal-reseed` instead. Anything changed in the demo (new accounts, requests) is gone after the reseed.
- **Hourly expiry job.** The container runs `ops/nightly_expiry.sh` every hour as the `vytal_scheduler` login, as `ops/crontab.example` does on a server. Units past their expiry time are never counted as stock, even between runs; the job marks them Expired, records the wastage, opens the stored low-stock alerts and tops up short requests.
- **Proxy address.** Behind Caddy (`enable-https.sh`, and `deploy-vps.sh` once HTTPS is set up) the container runs with `VYTAL_TRUST_PROXY=1`, so the sign-in throttle counts failed attempts per real client. The plain-HTTP run passes `VYTAL_TRUST_PROXY=0`: with no proxy in front, a client could send any `X-Forwarded-For` it likes, so the header is ignored.
- **Clinical rules are read-only.** With `VYTAL_DEMO=1` the eligibility minimums, donation intervals, shelf life, compatibility and deferral reasons cannot be changed, so one visitor cannot make every donor read as ineligible for the next. Stock minimums can still be changed.
- **Secure cookies.** The container sets `VYTAL_SECURE_COOKIE=1`, so the session cookie is `Secure; HttpOnly; SameSite=Lax` behind HTTPS. Before HTTPS is set up, `deploy-vps.sh` passes `-e VYTAL_SECURE_COOKIE=0`, because a browser never sends a Secure cookie back over plain HTTP and sign-in would not stick.

| Variable | Default in the container | Meaning |
|---|---|---|
| `VYTAL_SECURE_COOKIE` | `1` | `1` marks the session cookie Secure (HTTPS only), `0` for a plain-HTTP run |
| `VYTAL_TRUST_PROXY` | same as `VYTAL_SECURE_COOKIE` | `1` lets the sign-in throttle use the client address the proxy (Caddy) appends to `X-Forwarded-For`; `0` ignores the header. Set `1` only when the proxy is the only way to reach the app |
| `VYTAL_EXPIRY_EVERY` | `3600` | Seconds between runs of the expiry job inside the container |
| `VYTAL_SECRET` | random per start | Flask session key; a restart signs everyone out |
| `PORT` | `7860` | Port gunicorn listens on inside the container |

