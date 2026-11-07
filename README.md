# toonami-aftermath-dvr

A small Docker container that publishes two feeds for your DVR / IPTV app and keeps them fresh:

| URL | What |
|---|---|
| `http://HOST:8787/toonami.m3u` | Playlist: East, West, Movies, Radio |
| `http://HOST:8787/toonami.xml` | XMLTV guide, rebuilt every hour (default) |
| `http://HOST:8787/status.json` | Last refresh time, programme count, last error |
| `http://HOST:8787/healthz` | 200 when the guide is fresh (used by Docker's healthcheck) |

Python standard library only. Nothing to `pip install`.

## Run it

```bash
git clone https://github.com/Fofer/toonami-aftermath-dvr.git
cd toonami-aftermath-dvr
docker compose up -d --build
curl http://localhost:8787/status.json
```

Then in your DVR, add the playlist URL as a custom M3U source and the XML URL as its guide source.
The M3U carries `tvg-id` values that match the XMLTV channel ids, so channels map automatically.
Use the host's LAN IP or hostname rather than `localhost` when the DVR runs on another machine.
The URL written inside the M3U follows whatever host you used to fetch it; set `TA_PUBLIC_URL` to pin it (for example behind a reverse proxy).

Prebuilt multi-arch images (amd64 + arm64) are published to GHCR by the included GitHub Action, so you can use
`image: ghcr.io/Fofer/toonami-aftermath-dvr:latest` in `docker-compose.yml` instead of building.

## How the guide is built

The schedule API only returns the next handful of shows per request, so the service probes
`/channelsCurrentMedia?startDate=...` at several points (every `TA_SAMPLE_MINUTES`) across the horizon, merges and
de-duplicates, and sets each programme's stop time to the next programme's start. The last programme in a channel has no
successor, so it gets a stop of start + `TA_LAST_PROGRAMME_MINUTES`; that value is a placeholder, not API data.

Channels with no schedule data (Radio, and Movies if the API does not list it under a name in `channels.json`) get
rolling placeholder blocks so DVR apps do not show an empty guide. If the API is unreachable, the last good guide keeps
being served; on a first start with no cached guide you get placeholder-only data until the next successful refresh.

## Configuration

Environment variables (see `docker-compose.yml`):

| Variable | Default | Meaning |
|---|---|---|
| `TA_REFRESH_MINUTES` | 60 | Rebuild interval (min 5) |
| `TA_HORIZON_HOURS` | 24 | How far ahead the guide reaches |
| `TA_SAMPLE_MINUTES` | 120 | Probe spacing; keep it well under the span one API response covers |
| `TA_REQUEST_DELAY` | 0.5 | Seconds between API calls |
| `TA_PUBLIC_URL` | unset | Base URL written into the M3U |
| `TA_FILLER_HOURS` | 3 | Placeholder block length |
| `TA_PORT` | 8787 | Listen port |
| `TA_LOG_LEVEL` | INFO | Log verbosity |

Edit `channels.json` (mount it into `/app/channels.json`) to add or remove channels, set logos, or change numbering.
`api_names` lists the channel names as the schedule API spells them; leave it empty for streams without a schedule.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

## Publish to GitHub

```bash
git init -b main && git add . && git commit -m "Initial commit"
gh repo create toonami-aftermath-dvr --public --source=. --push
```

The workflow in `.github/workflows/docker.yml` runs the tests and pushes the image to GHCR on every push to `main`.
After the first run, set the package visibility to public under the repo's Packages settings if you want unauthenticated pulls.

## Notes

- Stream URLs point straight at the Toonami Aftermath servers; this container does not proxy video.
- Unofficial project, not affiliated with Toonami Aftermath. Check the service's own terms before redistributing its feeds publicly.
- The guide generator is an independent implementation based on the API's JSON shape; the idea of building XMLTV from `channelsCurrentMedia` was inspired by
  [Barrel0Memes97/Toonami-Aftermath-API-to-XMLTV-guide](https://github.com/Barrel0Memes97/Toonami-Aftermath-API-to-XMLTV-guide).
