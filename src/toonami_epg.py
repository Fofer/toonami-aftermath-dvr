#!/usr/bin/env python3
"""Toonami Aftermath playlist + XMLTV guide server (Python 3, stdlib only).

Serves, and keeps refreshed in the background:
  /toonami.m3u   IPTV playlist (generated per request, so URLs match the host you use)
  /toonami.xml   XMLTV guide built from the Toonami Aftermath schedule API
  /healthz       200 when a recent guide exists, else 503
  /status.json   refresh status / counters

API shape this relies on (observed from the public API):
  GET {API_BASE}/channelsCurrentMedia?startDate=<ISO-8601 UTC>
  -> [{"name": "...", "media": [{"name", "startDate", "blockName", "mediaType",
       "episodeNumber", "info": {"fullname", "episode", "image", "year"}}, ...]}, ...]
Each response only covers the next handful of shows, so we probe at several
startDate values and merge the results.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

LOG = logging.getLogger("toonami-epg")


def _int(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.getenv(name, default)))
    except ValueError:
        return default


class Config:
    def __init__(self) -> None:
        self.api_base = os.getenv("TA_API_BASE", "https://api.toonamiaftermath.com").rstrip("/")
        self.data_dir = Path(os.getenv("TA_DATA_DIR", "/data"))
        self.channels_file = Path(os.getenv("TA_CHANNELS_FILE", "channels.json"))
        self.host = os.getenv("TA_HOST", "0.0.0.0")
        self.port = _int("TA_PORT", 8787, 1)
        self.public_url = os.getenv("TA_PUBLIC_URL", "").rstrip("/")
        self.refresh_minutes = _int("TA_REFRESH_MINUTES", 60, 5)
        self.horizon_hours = _int("TA_HORIZON_HOURS", 24, 1)
        self.sample_minutes = _int("TA_SAMPLE_MINUTES", 120, 15)
        self.request_delay = float(os.getenv("TA_REQUEST_DELAY", "0.5"))
        self.http_timeout = _int("TA_HTTP_TIMEOUT", 20, 1)
        self.filler_hours = _int("TA_FILLER_HOURS", 3, 1)
        self.last_programme_minutes = _int("TA_LAST_PROGRAMME_MINUTES", 30, 1)
        self.fixture = os.getenv("TA_FIXTURE", "")  # offline testing: path to saved API JSON


# ---------------------------------------------------------------- helpers
def iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def xml_stamp(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S +0000")


def parse_start(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return dt.astimezone(timezone.utc).replace(microsecond=0)


def load_channels(path: Path) -> list[dict]:
    channels = json.loads(path.read_text(encoding="utf-8"))
    seen = set()
    for ch in channels:
        for key in ("id", "name", "stream"):
            if not ch.get(key):
                raise ValueError(f"channel is missing '{key}': {ch}")
        if ch["id"] in seen:
            raise ValueError(f"duplicate channel id: {ch['id']}")
        seen.add(ch["id"])
        ch.setdefault("group", "Toonami Aftermath")
        ch.setdefault("api_names", [])
        ch.setdefault("logo", "")
    return channels


# ---------------------------------------------------------------- fetching
def read_api(cfg: Config, at: datetime):
    if cfg.fixture:
        return json.loads(Path(cfg.fixture).read_text(encoding="utf-8-sig"))
    url = cfg.api_base + "/channelsCurrentMedia?" + urllib.parse.urlencode({"startDate": iso_utc(at)})
    req = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "User-Agent": "toonami-aftermath-dvr/1.0 (EPG generator)",
    })
    with urllib.request.urlopen(req, timeout=cfg.http_timeout) as resp:
        return json.loads(resp.read().decode("utf-8-sig"))


def collect(payload, channels: list[dict], records: dict) -> None:
    if not isinstance(payload, list):
        raise ValueError("expected a top-level JSON array of channels")
    by_name = {}
    for ch in channels:
        for n in ch["api_names"]:
            by_name[n.strip().casefold()] = ch["id"]
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        cid = by_name.get(str(entry.get("name", "")).strip().casefold())
        media = entry.get("media")
        if not cid or not isinstance(media, list):
            continue
        for item in media:
            if not isinstance(item, dict):
                continue
            start = parse_start(item.get("startDate"))
            info = item.get("info") if isinstance(item.get("info"), dict) else {}
            title = info.get("fullname") or item.get("name")
            if start is None or not isinstance(title, str) or not title.strip():
                continue
            image = info.get("image")
            rec = {
                "start": start,
                "title": title.strip(),
                "subtitle": info["episode"].strip() if isinstance(info.get("episode"), str) else None,
                "episode": item.get("episodeNumber"),
                "block": item.get("blockName"),
                "type": item.get("mediaType"),
                "year": info.get("year"),
                "image": image if isinstance(image, str) and image.startswith(("http://", "https://")) else None,
            }
            old = records[cid].get(start)
            if old is None or sum(bool(v) for v in rec.values()) > sum(bool(v) for v in old.values()):
                records[cid][start] = rec


def collect_schedule(cfg: Config, channels: list[dict], start: datetime, end: datetime) -> dict:
    records = {ch["id"]: {} for ch in channels}
    if not any(ch["api_names"] for ch in channels):
        return records
    probe, ok = start, 0
    while probe <= end:
        try:
            collect(read_api(cfg, probe), channels, records)
            ok += 1
        except Exception as exc:  # network/JSON errors: keep going with other probes
            LOG.warning("API request for %s failed: %s", iso_utc(probe), exc)
        if cfg.fixture:
            break
        probe += timedelta(minutes=cfg.sample_minutes)
        if probe <= end and cfg.request_delay:
            time.sleep(cfg.request_delay)
    if ok == 0:
        raise RuntimeError("every API request failed")
    return records


# ---------------------------------------------------------------- outputs
def _text(parent, tag, value, **attrs):
    if value is None or str(value).strip() == "":
        return
    node = ET.SubElement(parent, tag, attrs)
    node.text = str(value)


def build_xml(cfg: Config, channels: list[dict], records: dict, start: datetime, end: datetime):
    root = ET.Element("tv", {
        "source-info-name": "Toonami Aftermath API",
        "source-info-url": cfg.api_base,
        "generator-info-name": "toonami-aftermath-dvr",
    })
    for ch in channels:
        node = ET.SubElement(root, "channel", {"id": ch["id"]})
        _text(node, "display-name", ch["name"])
        if ch["logo"]:
            ET.SubElement(node, "icon", {"src": ch["logo"]})

    total, filled = 0, []
    last_len = timedelta(minutes=cfg.last_programme_minutes)
    for ch in channels:
        entries = sorted(records[ch["id"]].values(), key=lambda r: r["start"])
        if not entries:
            filled.append(ch["id"])
            entries = filler(cfg, ch, start, end)
        for i, r in enumerate(entries):
            nxt = entries[i + 1]["start"] if i + 1 < len(entries) else None
            stop = nxt if nxt and nxt > r["start"] else r["start"] + last_len
            if r["start"] > end or stop <= start:
                continue
            prog = ET.SubElement(root, "programme", {
                "channel": ch["id"], "start": xml_stamp(r["start"]), "stop": xml_stamp(stop)})
            _text(prog, "title", r["title"], lang="en")
            _text(prog, "sub-title", r.get("subtitle"), lang="en")
            _text(prog, "desc", r.get("desc") or (f"Programming block: {r['block']}" if r.get("block") else None), lang="en")
            _text(prog, "category", "Movie" if r.get("type") == "movie" else ch["group"], lang="en")
            if r.get("image"):
                ET.SubElement(prog, "icon", {"src": r["image"]})
            if isinstance(r.get("year"), int) and 1900 <= r["year"] <= 2100:
                _text(prog, "date", r["year"])
            if str(r.get("episode")).isdigit():
                _text(prog, "episode-num", r["episode"], system="onscreen")
            total += 1
    return ET.ElementTree(root), total, filled


def filler(cfg: Config, ch: dict, start: datetime, end: datetime) -> list[dict]:
    """Rolling placeholder blocks for channels with no schedule data (e.g. radio)."""
    step = timedelta(hours=cfg.filler_hours)
    epoch = datetime(2000, 1, 1, tzinfo=timezone.utc)
    t = epoch + ((start - epoch) // step) * step
    out = []
    while t <= end:
        out.append({"start": t, "title": ch["name"], "subtitle": None, "block": None,
                    "desc": "Live stream. No schedule data is published for this channel.",
                    "type": None, "year": None, "image": None, "episode": None})
        t += step
    return out


def build_m3u(channels: list[dict], base_url: str) -> str:
    lines = [f'#EXTM3U url-tvg="{base_url}/toonami.xml" x-tvg-url="{base_url}/toonami.xml"']
    for n, ch in enumerate(channels, 1):
        attrs = f'tvg-id="{ch["id"]}" tvg-name="{ch["name"]}" tvg-chno="{ch.get("number", n)}"'
        if ch["logo"]:
            attrs += f' tvg-logo="{ch["logo"]}"'
        attrs += f' group-title="{ch["group"]}"'
        lines += [f'#EXTINF:-1 {attrs},{ch["name"]}', ch["stream"]]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- state / refresh
class State:
    def __init__(self, cfg: Config, channels: list[dict]) -> None:
        self.cfg, self.channels = cfg, channels
        self.lock = threading.Lock()
        self.xml: bytes | None = None
        self.updated: datetime | None = None
        self.last_error: str | None = None
        self.programmes = 0
        self.filler_channels: list[str] = []
        self.degraded = False
        self.xml_path = cfg.data_dir / "toonami.xml"

    def load_cached(self) -> None:
        try:
            self.xml = self.xml_path.read_bytes()
            self.updated = datetime.fromtimestamp(self.xml_path.stat().st_mtime, timezone.utc)
            LOG.info("loaded cached guide from %s", self.xml_path)
        except OSError:
            pass

    def refresh(self) -> None:
        cfg = self.cfg
        now = datetime.now(timezone.utc)
        start, end = now - timedelta(hours=1), now + timedelta(hours=cfg.horizon_hours)
        degraded = False
        try:
            records = collect_schedule(cfg, self.channels, start, end)
        except Exception as exc:
            self.last_error = f"{iso_utc(now)}: {exc}"
            LOG.error("refresh failed: %s", exc)
            if self.xml is not None:
                return  # keep serving the last good guide
            records = {ch["id"]: {} for ch in self.channels}  # first run: placeholder-only guide
            degraded = True
        tree, total, filled = build_xml(cfg, self.channels, records, start, end)
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix="toonami-", suffix=".xml", dir=cfg.data_dir)
        os.close(fd)
        try:
            tree.write(tmp, encoding="utf-8", xml_declaration=True)
            ET.parse(tmp)  # sanity check before swapping in
            data = Path(tmp).read_bytes()
            os.replace(tmp, self.xml_path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        with self.lock:
            self.xml, self.updated = data, now
            self.programmes, self.filler_channels, self.degraded = total, filled, degraded
            if not degraded:
                self.last_error = None
        LOG.info("guide updated: %d programmes; placeholder-only channels: %s", total, filled or "none")

    def healthy(self) -> bool:
        return (self.xml is not None and self.updated is not None and not self.degraded and
                datetime.now(timezone.utc) - self.updated < timedelta(minutes=self.cfg.refresh_minutes * 3))

    def status(self) -> dict:
        return {"healthy": self.healthy(), "degraded": self.degraded,
                "updated": iso_utc(self.updated) if self.updated else None,
                "programmes": self.programmes, "placeholder_channels": self.filler_channels,
                "last_error": self.last_error, "refresh_minutes": self.cfg.refresh_minutes}


def refresh_loop(state: State, stop: threading.Event) -> None:
    while not stop.is_set():
        state.refresh()
        wait = state.cfg.refresh_minutes * 60 if not state.last_error else min(300, state.cfg.refresh_minutes * 60)
        stop.wait(wait)


def make_handler(state: State):
    class Handler(BaseHTTPRequestHandler):
        server_version = "toonami-epg"

        def log_message(self, fmt, *args):
            LOG.debug("%s %s", self.address_string(), fmt % args)

        def _send(self, code, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _base(self) -> str:
            if state.cfg.public_url:
                return state.cfg.public_url
            proto = self.headers.get("X-Forwarded-Proto", "http")
            return f"{proto}://{self.headers.get('Host', f'localhost:{state.cfg.port}')}"

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            path = urllib.parse.urlparse(self.path).path
            if path in ("/toonami.m3u", "/playlist.m3u"):
                self._send(200, build_m3u(state.channels, self._base()).encode(), "audio/x-mpegurl; charset=utf-8")
            elif path in ("/toonami.xml", "/guide.xml"):
                with state.lock:
                    xml = state.xml
                if xml is None:
                    self._send(503, b"guide not generated yet\n", "text/plain")
                else:
                    self._send(200, xml, "application/xml; charset=utf-8")
            elif path == "/healthz":
                ok = state.healthy()
                self._send(200 if ok else 503, b"ok\n" if ok else b"unhealthy\n", "text/plain")
            elif path == "/status.json":
                self._send(200, json.dumps(state.status(), indent=2).encode(), "application/json")
            elif path == "/":
                b = self._base()
                page = (f"<h3>Toonami Aftermath DVR feeds</h3><ul>"
                        f'<li><a href="{b}/toonami.m3u">{b}/toonami.m3u</a> (playlist)</li>'
                        f'<li><a href="{b}/toonami.xml">{b}/toonami.xml</a> (XMLTV guide)</li>'
                        f'<li><a href="{b}/status.json">status</a></li></ul>')
                self._send(200, page.encode(), "text/html; charset=utf-8")
            else:
                self._send(404, b"not found\n", "text/plain")

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", nargs="?", choices=("serve", "generate"), default="serve",
                        help="serve (default): refresh loop + HTTP; generate: write the guide once and exit")
    args = parser.parse_args()
    logging.basicConfig(level=os.getenv("TA_LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    cfg = Config()
    state = State(cfg, load_channels(cfg.channels_file))
    if args.command == "generate":
        state.refresh()
        return 0 if state.last_error is None else 1
    state.load_cached()
    stop = threading.Event()
    threading.Thread(target=refresh_loop, args=(state, stop), daemon=True).start()
    server = ThreadingHTTPServer((cfg.host, cfg.port), make_handler(state))
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown).start())
    LOG.info("listening on %s:%d, refreshing every %d min", cfg.host, cfg.port, cfg.refresh_minutes)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    stop.set()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
