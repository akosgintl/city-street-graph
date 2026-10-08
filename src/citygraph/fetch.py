"""Download a city's PBF from the config's `download:` sources (tried in order, stdlib only).

  download:
    - {url: https://…/City.osm.pbf}                                   # saved as the config's `pbf:`
    - {url: https://…/region-latest.osm.pbf, md5: https://….md5, clip: true}   # true = config bbox, or [W, S, E, N]

clip sources are kept in data/raw/cache/ (reused until --refresh) and cut with `citygraph clip`.
data/raw/<name>.source.json records where the PBF came from; build copies it into stats.json.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path
from urllib.parse import urlparse

from . import osm_load

TIMEOUT_S = 60
RETRIES = 3


class FetchError(Exception):
    pass


def source_path(pbf: Path) -> Path:
    return pbf.with_name(pbf.name.removesuffix(".osm.pbf").removesuffix(".pbf") + ".source.json")


def _open(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": f"citygraph/{version('citygraph')} (OSM street graph builder)"})
    host = urlparse(url).netloc
    for attempt in range(RETRIES + 1):
        try:
            return urllib.request.urlopen(req, timeout=TIMEOUT_S)
        except urllib.error.HTTPError as e:
            if e.code < 500 or attempt == RETRIES:
                hint = " - blocked by a proxy/firewall?" if e.code in (401, 403, 407) else ""
                raise FetchError(f"HTTP {e.code} from {host}{hint}") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            reason = str(getattr(e, "reason", e))
            if "403" in reason or "407" in reason:  # proxy refused the CONNECT tunnel
                raise FetchError(f"{reason} for {host} - blocked by a proxy/firewall?") from None
            if attempt == RETRIES:
                raise FetchError(f"{host}: {reason}") from None
        time.sleep(2 ** (attempt + 1))


def download(url: str, dst: Path, md5_url: str | None = None) -> dict:
    """Stream url to dst (via dst.part, atomic rename). Verifies the md5 when md5_url is given."""
    want = None
    if md5_url:
        with _open(md5_url) as r:
            want = r.read().decode("ascii", "replace").split()[0].lower()
    dst.parent.mkdir(parents=True, exist_ok=True)
    part = dst.with_name(dst.name + ".part")
    md5, sha, n = hashlib.md5(), hashlib.sha256(), 0
    try:
        with _open(url) as r, open(part, "wb") as f:
            total, step = int(r.headers.get("Content-Length") or 0), 5
            modified = r.headers.get("Last-Modified")
            while chunk := r.read(1 << 20):
                f.write(chunk)
                md5.update(chunk)
                sha.update(chunk)
                n += len(chunk)
                if total and n * 100 >= step * total:
                    print(f"  {urlparse(url).netloc}: {n * 100 // total}% of {total / 1e6:.0f} MB", flush=True)
                    step = n * 100 // total + 5
        if want and md5.hexdigest() != want:
            raise FetchError(f"md5 mismatch for {url}: got {md5.hexdigest()}, expected {want}")
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    os.replace(part, dst)
    return {"url": url, "last_modified": modified, "md5": md5.hexdigest(), "md5_verified": bool(want),
            "download_sha256": sha.hexdigest(), "download_bytes": n}


def fetch(cfg: dict, root: Path, pbf: Path, refresh: bool = False) -> dict:
    """Make sure `pbf` exists: try the `download:` sources in order. -> source record."""
    if pbf.is_file() and not refresh:
        sp = source_path(pbf)
        return json.loads(sp.read_text(encoding="utf-8")) if sp.is_file() else {}
    sources = cfg.get("download") or []
    if not sources:
        raise SystemExit(f"PBF not found: {pbf} and the config has no `download:` sources - see README > Get data")
    errors = []
    for src in sources:
        url, clip = src["url"], src.get("clip", False)
        print(f"fetch {url}" + (" (then clip)" if clip else ""), flush=True)
        try:
            if clip:
                region = root / "data" / "raw" / "cache" / Path(urlparse(url).path).name
                if refresh or not region.is_file():
                    rec = download(url, region, src.get("md5"))
                else:
                    rec = {"url": url, "cached": str(region.relative_to(root))}
                bbox = cfg["bbox"] if clip is True else clip
                tmp = pbf.with_name(pbf.name + ".part.osm.pbf")
                try:
                    osm_load.clip(region, tmp, bbox)
                except RuntimeError as e:  # unreadable download
                    tmp.unlink(missing_ok=True)
                    raise FetchError(f"clip failed: {e}") from None
                os.replace(tmp, pbf)
                rec["clipped_to"] = bbox
            else:
                rec = download(url, pbf, src.get("md5"))
        except FetchError as e:
            errors.append(f"  {url}: {e}")
            print(f"  failed: {e}", flush=True)
            continue
        rec.update({"sha256": osm_load.sha256(pbf), "bytes": pbf.stat().st_size,
                    "osm_timestamp": osm_load.header(pbf)["osm_timestamp"],
                    "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
        source_path(pbf).write_text(json.dumps(rec, indent=1) + "\n", encoding="utf-8")
        return rec
    raise SystemExit("all download sources failed:\n" + "\n".join(errors) + f"\nDownload a PBF manually to {pbf}")
