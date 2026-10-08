"""citygraph fetch against a local HTTP server (no internet needed)."""
import functools
import hashlib
import json
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest

from citygraph import fetch, osm_load
from test_smoke import CFG, write_osm


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    www = tmp_path_factory.mktemp("www")
    write_osm(www / "test.osm")
    osm_load.clip(www / "test.osm", www / "region.osm.pbf", [18.9, 47.4, 19.1, 47.6])  # tiny "region" PBF
    md5 = hashlib.md5((www / "region.osm.pbf").read_bytes()).hexdigest()
    (www / "region.osm.pbf.md5").write_text(f"{md5}  region.osm.pbf\n")
    (www / "bad.md5").write_text("0" * 32 + "  region.osm.pbf\n")
    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(www))
    handler.log_message = lambda *a: None
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


def test_fetch(server, tmp_path, monkeypatch):
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    pbf = tmp_path / "data/raw/test.osm.pbf"
    bad = {**CFG, "download": [{"url": f"{server}/region.osm.pbf", "md5": f"{server}/bad.md5"}]}
    with pytest.raises(SystemExit, match="md5 mismatch"):
        fetch.fetch(bad, tmp_path, pbf)
    assert not list(pbf.parent.iterdir())                                # nothing left behind
    cfg = {**CFG, "download": [{"url": f"{server}/missing.osm.pbf"},     # 404 -> next source
                               {"url": f"{server}/region.osm.pbf", "md5": f"{server}/region.osm.pbf.md5"}]}
    rec = fetch.fetch(cfg, tmp_path, pbf)
    assert rec["url"].endswith("/region.osm.pbf") and rec["md5_verified"] and rec["sha256"] == osm_load.sha256(pbf)
    assert json.loads(fetch.source_path(pbf).read_text()) == rec
    clip = {**CFG, "download": [{"url": f"{server}/region.osm.pbf", "clip": [18.999, 47.499, 19.0011, 47.5011]}]}
    rec = fetch.fetch(clip, tmp_path, pbf, refresh=True)
    assert rec["clipped_to"] and (tmp_path / "data/raw/cache/region.osm.pbf").is_file()
    d = osm_load.load(pbf, CFG["bbox"], "drive", None, set())
    assert {w for w, _, _ in d.ways} == {100, 101, 102, 110}
