<!-- DEPLOY_RAILWAY.md -->
# Deploying VibeChat to Railway

1. **Push the project to GitHub** (`.env` and `staticfiles/` are git-ignored; never commit `.env`).
2. **New Project → Deploy from GitHub repo.** Railway reads `railway.toml`, installs `requirements.txt`
   (Python 3.12 from `.python-version`) and runs migrate → collectstatic → Daphne on `$PORT`.
3. **Database:** either add a Railway *PostgreSQL* service and set the variable
   `DATABASE_URL=${{Postgres.DATABASE_URL}}`, or keep your Neon URL as `DATABASE_URL`.
4. **Variables** (service → Variables):
   | Variable | Value |
   |---|---|
   | `SECRET_KEY` | a new long random string (the app refuses to start without it) |
   | `DATABASE_URL` | see step 3 |
   | `CALL_TURN_URLS`, `CALL_TURN_SECRET` (or `CALL_TURN_USERNAME` / `CALL_TURN_CREDENTIAL`) | from a hosted TURN provider, for video calls |

   `DEBUG` is off automatically on Railway. `ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS` are filled from
   Railway's public domain; set them yourself only for a custom domain (e.g. `ALLOWED_HOSTS=chat.example.com`).
5. **Profile photos:** service → *Settings → Volumes → New Volume* (any mount path, e.g. `/data`).
   Photos are then stored in `<volume>/media` and survive redeploys.
6. **Networking:** service → *Settings → Networking → Generate Domain*. HTTPS and `wss://` work out of the box.

## Notes
- **Keep one replica.** Presence, calls and watch-together live in memory. To scale, add a Railway *Redis*
  service and set `REDIS_URL=${{Redis.REDIS_URL}}`.
- **Video calls:** Railway cannot host a UDP TURN server; use a hosted one (Metered, Twilio, Cloudflare…)
  with `turn:`/`turns:` over TCP, e.g. `CALL_TURN_URLS=turns:turn.example.com:443?transport=tcp`.
- **Errors** appear in the service's *Deploy Logs* (set `LOG_LEVEL=INFO` for more detail).
- **Admin user:** in the service shell run `python manage.py createsuperuser`.
- **Rotate secrets** that were ever shared outside your machine (for example the Neon password).
