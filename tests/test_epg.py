import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
import toonami_epg as t  # noqa: E402


def fixture(now):
    def item(name, mins, **kw):
        d = {"name": name, "startDate": (now + timedelta(minutes=mins)).strftime("%Y-%m-%dT%H:%M:%S.123Z"),
             "blockName": "Toonami", "mediaType": "series", "episodeNumber": 7,
             "info": {"fullname": name + " Full", "episode": "Ep", "year": 1999, "image": "https://x/y.gif"}}
        d.update(kw)
        return d
    return [
        {"name": "Toonami Aftermath East", "media": [item("A", 0), item("B", 25), item("A", 0), item("C", 50)]},
        {"name": "Toonami Aftermath West", "media": [item("D", 5)]},
        {"name": "Movies", "media": [item("Film", 10, mediaType="movie", info={})]},
        {"name": "Unrelated", "media": [item("Z", 1)]},
    ]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = datetime.now(timezone.utc)
        fx = Path(self.tmp.name) / "fx.json"
        fx.write_text(json.dumps(fixture(self.now)))
        os.environ.update(TA_FIXTURE=str(fx), TA_DATA_DIR=self.tmp.name, TA_REQUEST_DELAY="0")
        self.cfg = t.Config()
        self.channels = t.load_channels(ROOT / "channels.json")
        self.state = t.State(self.cfg, self.channels)


class GuideTests(Base):
    def test_guide_contents(self):
        self.state.refresh()
        root = ET.fromstring(self.state.xml)
        self.assertEqual([c.get("id") for c in root.findall("channel")],
                         [c["id"] for c in self.channels])
        east = [p for p in root.findall("programme") if p.get("channel") == "toonami-aftermath-east"]
        self.assertEqual(len(east), 3)  # duplicate "A" collapsed
        self.assertEqual(east[0].get("stop"), east[1].get("start"))  # stop = next start
        self.assertEqual(east[0].find("title").text, "A Full")
        self.assertTrue(all(p.get("stop") for p in root.findall("programme")))
        movie = [p for p in root.findall("programme") if p.get("channel") == "toonami-aftermath-movies"][0]
        self.assertEqual(movie.find("category").text, "Movie")
        self.assertFalse(any(p.find("title").text == "Z Full" for p in root.findall("programme")))

    def test_radio_gets_placeholder_blocks(self):
        self.state.refresh()
        root = ET.fromstring(self.state.xml)
        radio = [p for p in root.findall("programme") if p.get("channel") == "toonami-aftermath-radio"]
        self.assertGreaterEqual(len(radio), 8)  # 24h / 3h
        self.assertEqual(self.state.filler_channels, ["toonami-aftermath-radio"])

    def test_api_failure_keeps_last_good_guide(self):
        self.state.refresh()
        good = self.state.xml
        os.environ["TA_FIXTURE"] = "/nonexistent.json"
        self.state.cfg = t.Config()
        self.state.refresh()
        self.assertEqual(self.state.xml, good)
        self.assertIsNotNone(self.state.last_error)

    def test_first_run_api_failure_serves_placeholders(self):
        os.environ["TA_FIXTURE"] = "/nonexistent.json"
        self.state.cfg = t.Config()
        self.state.refresh()
        self.assertTrue(self.state.degraded)
        self.assertFalse(self.state.healthy())
        self.assertIsNotNone(self.state.xml)


class HttpTests(Base):
    def test_endpoints(self):
        self.state.refresh()
        srv = ThreadingHTTPServer(("127.0.0.1", 0), t.make_handler(self.state))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        m3u = urllib.request.urlopen(base + "/toonami.m3u").read().decode()
        self.assertIn(f'url-tvg="{base}/toonami.xml"', m3u)
        self.assertEqual(m3u.count("#EXTINF"), 4)
        self.assertIn("http://api.toonamiaftermath.com:3000/est/playlist.m3u8", m3u)
        ET.fromstring(urllib.request.urlopen(base + "/toonami.xml").read())
        self.assertEqual(urllib.request.urlopen(base + "/healthz").status, 200)
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(base + "/nope")


if __name__ == "__main__":
    unittest.main()
