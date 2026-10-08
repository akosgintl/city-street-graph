"""Read an OSM PBF with pyosmium: street ways, river geometry, admin areas (+ clip / info helpers)."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

import osmium
import shapely
from osmium.filter import KeyFilter, TagFilter
from shapely.geometry import LineString

# Overpass-style exclusion filters (unanchored regex on the tag value), mirroring OSMnx 2.x.
# 'drive' additionally drops access=no (OSMnx only drops access=private).
_HW_DRIVE = ("abandoned|bridleway|bus_guideway|construction|corridor|cycleway|elevator|escalator|footway|no|"
             "path|pedestrian|planned|platform|proposed|raceway|razed|rest_area|service|services|steps|track")
NETWORK_FILTERS = {
    "drive": {"highway": _HW_DRIVE, "area": "yes", "access": "private|^no$", "motor_vehicle": "no",
              "motorcar": "no", "service": "alley|driveway|emergency_access|parking|parking_aisle|private"},
    "walk": {"highway": "abandoned|bus_guideway|construction|cycleway|motor|no|planned|platform|proposed|"
                        "raceway|razed|rest_area|services",
             "area": "yes", "access": "private", "foot": "no", "service": "private", "sidewalk": "separate",
             "sidewalk:both": "separate", "sidewalk:left": "separate", "sidewalk:right": "separate"},
    "all": {"highway": "abandoned|construction|no|planned|platform|proposed|raceway|razed|rest_area|services",
            "area": "yes"},
}
NETWORK_FILTERS["drive_osmnx"] = {**NETWORK_FILTERS["drive"], "access": "private"}  # exact OSMnx 2.1.1 drive filter
WAY_TAGS = ("name", "highway", "oneway", "lanes", "maxspeed", "bridge", "bridge:name", "tunnel", "junction", "ref",
            "access", "area", "est_width", "landuse", "service", "width")  # last row: rest of OSMnx useful_tags_way
NODE_TAGS = ("highway", "junction", "railway", "ref")                       # OSMnx useful_tags_node
# named bridges kept as context next to a river: carriageways (whatever the network filter says) and railways
_BRIDGE_ROAD = re.compile("^(motorway|trunk|primary|secondary|tertiary|unclassified|residential|living_street)")
_BRIDGE_RAIL = ("rail", "light_rail", "subway", "narrow_gauge")


@dataclass
class OsmData:
    ways: list[tuple[int, list[int], dict]] = field(default_factory=list)  # (way id, node refs, kept tags)
    coords: dict[int, tuple[float, float]] = field(default_factory=dict)  # node id -> (lon, lat)
    signals: set[int] = field(default_factory=set)                         # highway=traffic_signals nodes
    node_tags: dict[int, dict] = field(default_factory=dict)               # NODE_TAGS of street nodes
    river_lines: list[LineString] = field(default_factory=list)            # waterway=river centerline ways
    water_areas: list = field(default_factory=list)                        # natural=water+water=river / riverbank
    bridge_ways: list[tuple[int, str, str, LineString]] = field(default_factory=list)  # (way id, bridge name,
                                                                           # road|rail, line); only with a river
    admin: dict[tuple[str, str], list] = field(default_factory=dict)       # (admin_level, name) -> [(relation id,
                                                                           # (Multi)Polygon)], only those touching bbox


def keep_way(tags, network: str) -> bool:
    if "highway" not in tags:
        return False
    return not any(k in tags and re.search(rx, tags[k]) for k, rx in NETWORK_FILTERS[network].items())


def load(pbf: Path, bbox: list[float], network: str, river: str | None, admin_levels: set[str],
         include_names: set[str] = frozenset()) -> OsmData:
    """One pass over the file. Keeps every filtered highway way with at least one node inside bbox.
    Ways named in include_names are kept even if the network filter drops them (tagged restricted)."""
    w, s, e, n = bbox
    d = OsmData()
    wkb = osmium.geom.WKBFactory()
    fp = (osmium.FileProcessor(str(pbf)).with_locations()
          .with_areas(TagFilter(("boundary", "administrative"), ("natural", "water"), ("waterway", "riverbank")))
          .with_filter(KeyFilter("highway", "waterway", "natural", "boundary", "railway", "junction", "ref")))
    for o in fp:
        t = o.tags
        if o.is_node():
            if t.get("highway") == "traffic_signals":
                d.signals.add(o.id)
            nt = {k: t[k] for k in NODE_TAGS if k in t}
            if nt:
                d.node_tags[o.id] = nt
        elif o.is_way():
            if river and t.get("waterway") == "river" and t.get("name") == river:
                try:
                    d.river_lines.append(LineString([(x.lon, x.lat) for x in o.nodes]))
                except osmium.InvalidLocationError:
                    pass
            bname = t.get("bridge:name") or t.get("name")
            if river and bname and t.get("bridge", "no") != "no":
                kind = ("rail" if t.get("railway") in _BRIDGE_RAIL
                        else "road" if _BRIDGE_ROAD.match(t.get("highway", "")) else None)
                try:
                    if kind and len(o.nodes) > 1:
                        d.bridge_ways.append((o.id, bname, kind, LineString([(x.lon, x.lat) for x in o.nodes])))
                except osmium.InvalidLocationError:
                    pass
            restricted = False
            if not keep_way(t, network):
                if t.get("name") not in include_names or not keep_way(t, "all"):
                    continue
                restricted = True
            try:
                pts = [(x.ref, x.lon, x.lat) for x in o.nodes]
            except osmium.InvalidLocationError:
                continue  # incomplete way at the extract edge
            if len(pts) < 2 or not any(w <= lon <= e and s <= lat <= n for _, lon, lat in pts):
                continue
            for ref, lon, lat in pts:
                d.coords[ref] = (lon, lat)
            d.ways.append((o.id, [p[0] for p in pts], {**{k: t[k] for k in WAY_TAGS if k in t}, "restricted": restricted}))
        elif o.is_area():
            water = (t.get("natural") == "water" and t.get("water") == "river") or t.get("waterway") == "riverbank"
            admin = (not o.from_way() and t.get("boundary") == "administrative"
                     and t.get("admin_level") in admin_levels and "name" in t)
            if water or admin:
                try:
                    g = shapely.from_wkb(wkb.create_multipolygon(o))
                except RuntimeError:
                    continue
                if water:
                    d.water_areas.append(g)
                x0, y0, x1, y1 = g.bounds
                if admin and x0 <= e and x1 >= w and y0 <= n and y1 >= s:  # names repeat across towns (Gràcia)
                    d.admin.setdefault((t["admin_level"], t["name"]), []).append((o.orig_id(), g))
    d.ways.sort(key=lambda x: x[0])
    d.bridge_ways.sort(key=lambda x: x[0])
    for v in d.admin.values():
        v.sort(key=lambda x: x[0])
    d.signals &= d.coords.keys()
    d.node_tags = {n: v for n, v in d.node_tags.items() if n in d.coords}
    return d


def header(pbf: Path) -> dict:
    r = osmium.io.Reader(str(pbf), osmium.osm.osm_entity_bits.NOTHING)
    h = r.header()
    box = h.box()
    out = {"file": Path(pbf).name, "bytes": Path(pbf).stat().st_size,
           "bbox": [box.bottom_left.lon, box.bottom_left.lat, box.top_right.lon, box.top_right.lat]
           if box.valid() else None,
           "osm_timestamp": h.get("osmosis_replication_timestamp") or h.get("timestamp") or None,
           "generator": h.get("generator") or None}
    r.close()
    return out


def sha256(path: Path) -> str:
    hs = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            hs.update(chunk)
    return hs.hexdigest()


def clip(src: Path, dst: Path, bbox: list[float]) -> None:
    """Cut src to bbox: nodes inside, ways touching them, admin/water relations touching those, plus all
    referenced objects (ways and relations stay complete). Keeps the replication timestamp."""
    w, s, e, n = bbox
    keep = osmium.IdTracker()
    nodes, ways = osmium.index.IdSet(), osmium.index.IdSet()
    rel_keys = ("boundary", "natural", "waterway", "water")
    for o in osmium.FileProcessor(str(src)):
        if o.is_node():
            if o.location.valid() and w <= o.lon <= e and s <= o.lat <= n:
                nodes.set(o.id)
                keep.add_node(o.id)
        elif o.is_way():
            if any(nodes.get(r.ref) for r in o.nodes):
                ways.set(o.id)
                keep.add_way(o.id)
                keep.add_references(o)
        elif o.is_relation():
            if any(k in o.tags for k in rel_keys) and any(
                    (m.type == "w" and ways.get(m.ref)) or (m.type == "n" and nodes.get(m.ref)) for m in o.members):
                keep.add_relation(o.id)
                keep.add_references(o)
    keep.complete_backward_references(str(src), relation_depth=0)
    src_hdr = osmium.io.Reader(str(src), osmium.osm.osm_entity_bits.NOTHING)
    hdr = osmium.io.Header()
    for k in ("osmosis_replication_timestamp", "osmosis_replication_sequence_number", "osmosis_replication_base_url"):
        if src_hdr.header().get(k):
            hdr.set(k, src_hdr.header().get(k))
    src_hdr.close()
    hdr.set("generator", "citygraph clip")
    hdr.add_box(osmium.osm.Box(osmium.osm.Location(w, s), osmium.osm.Location(e, n)))
    with osmium.SimpleWriter(str(dst), header=hdr, overwrite=True) as out:
        for o in osmium.FileProcessor(str(src)).with_filter(keep.id_filter()):
            out.add(o)
