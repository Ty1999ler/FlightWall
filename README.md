# FlightWall

A personal "flight wall": a live dashboard of the aircraft overhead right
now — flight number, airline, where each one is coming from and going to,
aircraft type, altitude, speed, distance, look-up angle, and a photo of the
actual airframe. No API keys, no accounts.

Runs two ways: locally on a PC, or as a Docker container on the NAS with the
same GitHub → Actions → ghcr.io → Watchtower pipeline as family-hub.

## Run it locally

```
py -m pip install -r requirements.txt
py server.py
```

Then open **http://127.0.0.1:8484**. On first run the server auto-detects your
location from your IP and writes it to `config.json`; fix it in ⚙ settings if
it guessed wrong.

## Settings (`config.json`, or the ⚙ gear in the app)

| Key               | Meaning                                            | Default    |
|-------------------|----------------------------------------------------|------------|
| `lat`, `lon`      | Home position (decimal degrees)                    | IP-detected|
| `location_name`   | Label shown in the header                          | IP city    |
| `radius_nm`       | Search radius in nautical miles (1–250)            | 30         |
| `refresh_seconds` | How often the wall refreshes (3–120)               | 8          |
| `units`           | `imperial` (mi/ft/kt) or `metric` (km/m/km/h)      | imperial   |
| `display_mode`    | `closest` (one big card) or `all` (grid of cards)  | closest    |

**Location source** (in settings) is per-device, not saved on the server:
*Home* uses the saved coordinates above; *This device — GPS* follows the
phone's live position (requires HTTPS — see Cloudflare below). GPS
coordinates are sent in request bodies only and are never written to disk.

## Deploying to the Synology (family-hub pattern)

Pipeline: push to `main` → GitHub Actions runs pytest (**failing tests block
the deploy**) → image builds and pushes to ghcr.io (private) → Watchtower on
the NAS picks it up within ~5 minutes.

One-time setup:

1. **GitHub**: create a private repo, push this project. The included
   `.github/workflows/build.yml` handles test + build + publish.
2. **NAS folder**: over SSH, create the folders **and make the data dir
   writable by the container user (uid 1000)** — without the chown, saved
   settings silently reset on every update:
   ```
   sudo mkdir -p /volume1/docker/flightwall/data
   sudo chown 1000 /volume1/docker/flightwall/data
   ```
   Then copy in `docker-compose.yml` (edit `GITHUB_USER/REPO` to the real
   image path, lowercase) and `.env` (from `.env.example`, with the tunnel
   token).
3. **Pull access**: the NAS `docker login ghcr.io` from the family-hub setup
   already covers pulling this private image.
4. **Start it**: from that folder, `sudo docker-compose up -d`.
   LAN URL: `http://NAS-IP:8484`. Health check: `/healthz`.
5. **Update the server map**: claim port 8484 and add the two containers to
   `server-map.md` in the Server repo.

The saved home location lives in `/volume1/docker/flightwall/data/config.json`
on the NAS (bind mount), so it survives image updates.

## Cloudflare Tunnel + Access (HTTPS, phone GPS, family-only)

Phone browsers only allow location services on HTTPS pages, so remote "planes
above me right now" needs the tunnel:

1. **Zero Trust dashboard → Networks → Tunnels → Create a tunnel** (Docker
   connector). Copy the token into `.env` as `TUNNEL_TOKEN`.
2. Add a **public hostname**: `flightwall.<your-domain>` →
   service `http://flightwall:8000` (the compose service name — cloudflared
   and the app share a network).
3. **Zero Trust → Access → Applications → Add self-hosted app** for that
   hostname, with an Allow policy listing family email addresses
   (one-time PIN login). This keeps the wall private — without it, the URL
   would be public and the app has no login of its own.
4. On phones: open `https://flightwall.<your-domain>`, sign in with the email
   PIN (Cloudflare re-prompts when the session expires — set the Access app's
   session duration to its maximum, e.g. 1 month, to keep this rare; the wall
   shows "Sign-in expired" when it happens), add to home screen (it installs
   as an app), then in ⚙ settings set Location source to *This device — GPS*.

## How it works

- **Live positions** — [adsb.lol](https://adsb.lol) `v2/point` API: community
  ADS-B receivers, aircraft within the radius, no key required.
- **Routes & airlines** — [adsbdb.com](https://adsbdb.com): callsign → airline
  and route; hex → aircraft type, registration, photo. Cached in memory.
- **Route sanity check** — adsbdb routes are keyed by callsign and can be
  stale; if the plane isn't plausibly near the origin→destination great-circle
  path the card shows **⚠ ROUTE UNVERIFIED**.
- **Look angle** — `atan(altitude ÷ ground distance)`; the OVERHEAD badge
  means 60°+ above the horizon, i.e. genuinely above your head.
- Click any card to open that aircraft on the ADS-B Exchange globe.

Coverage note: aircraft appear only if a community receiver hears them, and
some military/blocked aircraft transmit little or no identity data.
