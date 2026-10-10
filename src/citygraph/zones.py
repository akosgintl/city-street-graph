"""Zone polygons, ordered inside -> outside (config `zones:` list; zone_order = position). Entry types:

  osm_admin  boundary=administrative relations from the PBF by admin_level + name.
  osm_admin_auto  every admin_level relation mostly inside the `area:` polygon (or inside `within: {name,
             admin_level}`), minus the `exclude:` names, ordered by centroid distance from `order_from: [lon, lat]`;
             zone = ID-safe name (I_KERULET), zone_name = OSM name.
  geojson    polygons from a file (properties: zone, zone_order), e.g. a previous out/<city>/zones.geojson.
  ring       boundary road between two river bridge heads, closed along the river (see rings.py).

All geometry is in metres (local UTM). A zone's effective area is its polygon minus all inner zones.
"""
from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter
from pathlib import Path

from shapely.geometry import Point, shape

from . import rings


class Ctx:
    """What zone builders need from the graph (all metric)."""

    def __init__(self, segs, xy, river, centerline, admin, to_m, root: Path, area=None):
        self.segs, self.xy, self.river, self.centerline = segs, xy, river, centerline
        self.admin, self.to_m, self.root, self.area = admin, to_m, root, area
        self.names: dict[int, set[str]] = {}
        for s in segs:
            for n in (s["u"], s["v"]):
                self.names.setdefault(n, set()).update(s["names"])


def norm(name: str) -> str:
    """Loose name match: NFC, casefold, 'a - b' == 'a-b', collapsed spaces."""
    s = unicodedata.normalize("NFC", name).casefold().strip()
    return re.sub(r"\s+", " ", re.sub(r"\s*-\s*", "-", s))


def unique(gs: list, name: str, lvl: str):
    if len(gs) > 1:
        raise SystemExit(f"{name!r} (admin_level {lvl}) is ambiguous in the bbox: relations {[i for i, _ in gs]}")
    return gs[0][1]


def admin_key(spec: dict) -> str:
    """Key of a `{name, admin_level}` / `{name, boundary}` reference in OsmData.admin: the admin_level, or the
    boundary=* type for areas that are not boundary=administrative (e.g. local_authority)."""
    b = spec.get("boundary", "administrative")
    return str(spec["admin_level"]) if b == "administrative" else str(b)


def find_admin(admin: dict, spec: dict):
    """The one area called spec.name at spec's admin_level / boundary type (lon/lat)."""
    key = admin_key(spec)
    what = f"admin_level {key}" if key.isdigit() else f"boundary={key}"
    hit = [gs for (k, n), gs in sorted(admin.items()) if k == key and norm(n) == norm(spec["name"])]
    if not hit:
        raise SystemExit(f"area {spec['name']!r} ({what}) not in PBF; available: {sorted(n for k, n in admin if k == key)}")
    return unique(hit[0], spec["name"], key)


def osm_admin(ctx: Ctx, spec: dict) -> list[tuple]:
    lvl = str(spec["admin_level"])
    found = {norm(n): unique(gs, n, lvl) for (lv, n), gs in sorted(ctx.admin.items()) if lv == lvl}
    out = []
    for zone, alts in spec["areas"].items():
        alts = [alts] if isinstance(alts, str) else alts
        hit = next((found[norm(a)] for a in alts if norm(a) in found), None)
        if hit is None:
            have = sorted(n for lv, n in ctx.admin if lv == lvl)
            raise SystemExit(f"admin area {alts} (admin_level {lvl}) not in PBF; available: {have}")
        out.append((zone, "osm_admin", ctx.to_m(hit)))
    return out


def ident(name: str) -> str:
    """'XIII. kerület' -> 'XIII_KERULET', 'Sants-Montjuïc' -> 'SANTS_MONTJUIC'."""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_").upper()


def osm_admin_auto(ctx: Ctx, spec: dict) -> list[tuple]:
    parent = ctx.to_m(find_admin(ctx.admin, spec["within"])) if spec.get("within") else ctx.area
    if parent is None:
        raise SystemExit("osm_admin_auto zones need `area:` in the config (or `within:` in the zone entry)")
    lvl = str(spec["admin_level"])
    centre = ctx.to_m(Point(spec["order_from"]))
    skip = {norm(n) for n in spec.get("exclude") or []}
    found = []
    for (lv, name), gs in sorted(ctx.admin.items()):
        for _, g in gs if lv == lvl and norm(name) not in skip else []:
            gm = ctx.to_m(g)
            if gm.area > 0 and gm.intersection(parent).area > 0.5 * gm.area:
                found.append((round(gm.centroid.distance(centre), 3), name, gm))
    expect = spec.get("expect_count")
    if not found or (expect is not None and len(found) != expect):
        raise SystemExit(f"admin_level {lvl}: found {len(found)} areas inside the study area, expected "
                         f"{expect or '> 0'}: {sorted(n for _, n, _ in found)}")
    found.sort(key=lambda f: f[:2])
    ids = [ident(n) for _, n, _ in found]
    if len(set(ids)) != len(ids):
        raise SystemExit(f"admin_level {lvl} names are not unique as IDs: {ids}")
    return [(i, "osm_admin_auto", gm, n) for i, (_, n, gm) in zip(ids, found)]


def geojson(ctx: Ctx, spec: dict) -> list[tuple]:
    fc = json.loads((ctx.root / spec["file"]).read_text(encoding="utf-8"))
    feats = sorted((f for f in fc["features"] if "zone_order" in f["properties"]),
                   key=lambda f: (f["properties"]["zone_order"], f["properties"]["zone"]))
    return [(f["properties"]["zone"], "geojson", ctx.to_m(shape(f["geometry"]))) for f in feats]


def build(ctx: Ctx, specs: list[dict]) -> tuple[list[dict], dict]:
    """-> (zones [{zone, zone_order, source, geom}], ring info by zone)."""
    raw, ring_info = [], {}
    for spec in specs:
        t = spec.get("type", spec.get("source"))
        if t == "osm_admin":
            raw += osm_admin(ctx, spec)
        elif t == "osm_admin_auto":
            raw += osm_admin_auto(ctx, spec)
        elif t == "geojson":
            raw += geojson(ctx, spec)
        elif t == "ring":
            geom, ring_info[spec["zone"]] = rings.ring_zone(ctx, spec)
            raw.append((spec["zone"], "ring", geom))
        else:
            raise SystemExit(f"unknown zone type {t!r} (use osm_admin, osm_admin_auto, geojson or ring)")
    dup = sorted(z for z, c in Counter(r[0] for r in raw).items() if c > 1)
    if dup:
        raise SystemExit(f"zone ids are not unique: {dup}")
    zones, inner = [], None
    for i, (zone, src, geom, *name) in enumerate(raw):
        eff = geom if inner is None else geom.difference(inner)
        inner = geom if inner is None else inner.union(geom)
        zones.append({"zone": zone, "zone_order": i, "source": src, "geom": eff, **({"name": name[0]} if name else {})})
    return zones, ring_info
