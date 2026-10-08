"""PBF -> topological street graph -> zones -> clip -> checks -> exports."""
from __future__ import annotations

import json
import platform
from collections import Counter, defaultdict
from importlib.metadata import version
from pathlib import Path

import networkx as nx
import numpy as np
import shapely
from pyproj import Geod, Transformer
from shapely.geometry import LineString, Point

from . import export_neo4j, export_osmnx, fetch, osm_load, rings, zones as Z

GEOD = Geod(ellps="WGS84")
BRIDGE_NODE_MAX_M = 300.0  # unzoned bridge nodes over the river join the nearest zone within this distance
MAX_DROPPED_SHARE = 0.02   # warn when the parts outside the largest connected component exceed this node share
AREA_BUFFER_M = 500.0      # like OSMnx graph_from_polygon: simplify inside the area + 500 m, then truncate to the area


def utm_epsg(bbox) -> int:
    lon, lat = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
    return (32600 if lat >= 0 else 32700) + int((lon + 180) // 6) + 1


HIGHWAY_RANK = ["motorway", "trunk", "primary", "secondary", "tertiary", "motorway_link", "trunk_link",
                "primary_link", "secondary_link", "tertiary_link", "unclassified", "residential", "living_street",
                "service", "pedestrian", "footway", "cycleway", "path", "track", "steps"]


def pieces(data: osm_load.OsmData, network: str, osmnx: bool = False) -> list[dict]:
    """Split ways at nodes shared by >= 2 ways (or repeated in one way) and at way ends.
    Oneway pieces point in travel direction."""
    count, ends = Counter(), set()
    for _, refs, _ in data.ways:
        count.update(refs)
        ends.update((refs[0], refs[-1]))
    out, seqs = [], Counter()  # a way cut by truncate_ways() appears several times; ids stay unique
    for wid, refs, tags in data.ways:
        start = 0
        for i in range(1, len(refs)):
            if count[refs[i]] > 1 or refs[i] in ends:
                piece, start = refs[start:i + 1], i
                coords = [data.coords[r] for r in piece]
                length = GEOD.line_length(*zip(*coords))
                if length > 0:
                    out.append(make_piece(wid, seqs[wid], piece, coords, length, tags, network, osmnx))
                    seqs[wid] += 1
    return out


def make_piece(wid, seq, refs, coords, length, tags, network, osmnx=False) -> dict:
    ow = tags.get("oneway")
    if osmnx:  # OSMnx 2.1.1 _is_path_one_way / _is_path_reversed
        oneway = network != "walk" and (ow in ("yes", "true", "1", "-1", "reverse", "T", "F")
                                        or tags.get("junction") == "roundabout")
        backwards = ow in ("-1", "reverse", "T")
    else:
        oneway = network != "walk" and (ow in ("yes", "true", "1", "-1", "reverse") or (
            tags.get("junction") == "roundabout" and ow not in ("no", "false", "0")))
        backwards = ow in ("-1", "reverse")
    if oneway and backwards:
        refs, coords = refs[::-1], coords[::-1]
    name = tags.get("name")
    is_bridge = tags.get("bridge", "no") != "no"
    return {"id": f"{wid}-{seq}", "u": refs[0], "v": refs[-1], "way_id": wid, "refs": refs, "tags": tags,
            "coords": coords, "name": name,
            "highway": tags.get("highway"), "length": length, "oneway": oneway, "lanes": tags.get("lanes"),
            "maxspeed": tags.get("maxspeed"), "ref": tags.get("ref"), "junction": tags.get("junction"),
            "is_bridge": is_bridge, "bridge_name": (tags.get("bridge:name") or name) if is_bridge else None,
            "is_tunnel": tags.get("tunnel", "no") != "no", "restricted": tags["restricted"], "river_crossing": False}


def simplify(parts: list[dict], keep_way_endpoints: bool = False, osmnx: bool = False) -> list[dict]:
    """Merge pieces through pass-through nodes (OSMnx simplify_graph rule: exactly two incident pieces to two
    distinct neighbours, same oneway status, consistent direction). Ids/orientation follow the first piece.
    osmnx: neighbours are the adjacent OSM nodes (as OSMnx), not the far piece ends; differs only for 2-cycles."""
    at = defaultdict(list)
    for p in parts:
        at[p["u"]].append(p)
        at[p["v"]].append(p)

    def through(n) -> bool:
        if keep_way_endpoints or len(at[n]) != 2:
            return False
        a, b = at[n]
        nb = ([p["refs"][1] if p["u"] == n else p["refs"][-2] for p in (a, b)] if osmnx
              else [p["v"] if p["u"] == n else p["u"] for p in (a, b)])
        return (a is not b and a["oneway"] == b["oneway"] and nb[0] != nb[1] and n not in nb
                and (not a["oneway"] or (a["v"] == n) != (b["v"] == n)))

    used, segs = set(), []

    def walk(node, last) -> list:  # [(piece, walked_from_u)] leaving `node`, until a real node
        out = []
        while through(node):
            q = at[node][1] if at[node][0] is last else at[node][0]
            if q["id"] in used:
                break
            used.add(q["id"])
            f = q["u"] == node
            out.append((q, f))
            last, node = q, (q["v"] if f else q["u"])
        return out

    for p in parts:
        if p["id"] in used:
            continue
        used.add(p["id"])
        chain = [(q, not f) for q, f in reversed(walk(p["u"], p))] + [(p, True)] + walk(p["v"], p)
        segs.append(merge(p, chain))
    return segs


def merge(p, chain) -> dict:
    coords, coords_m = [], []
    for q, f in chain:
        c, cm = (q["coords"], list(q["geom_m"].coords)) if f else (q["coords"][::-1], list(q["geom_m"].coords)[::-1])
        coords += c if not coords else c[1:]
        coords_m += cm if not coords_m else cm[1:]
    qs = [q for q, _ in chain]
    uniq = lambda k: list(dict.fromkeys(q[k] for q in qs if q[k] is not None))  # noqa: E731
    names = uniq("name")
    by_len = Counter()
    for q in qs:
        if q["name"]:
            by_len[q["name"]] += q["length"]
    j = lambda k: ";".join(uniq(k)) or None  # noqa: E731
    crossing = [q["bridge_name"] for q in qs if q["river_crossing"] and q["bridge_name"]]
    bridge_name = crossing[0] if crossing else next(iter(uniq("bridge_name")), None)
    name = crossing[0] if crossing else max(names, key=lambda n: (by_len[n], -names.index(n))) if names else None
    return {"id": p["id"], "u": chain[0][0]["u"] if chain[0][1] else chain[0][0]["v"],
            "v": chain[-1][0]["v"] if chain[-1][1] else chain[-1][0]["u"], "way_id": p["way_id"],
            "coords": coords, "geom_m": LineString(coords_m),
            "name": name, "names": names,
            "highway": min(uniq("highway"), key=lambda h: HIGHWAY_RANK.index(h) if h in HIGHWAY_RANK else 99),
            "length_m": round(sum(q["length"] for q in qs), 3), "oneway": p["oneway"],
            "lanes": j("lanes"), "maxspeed": j("maxspeed"), "ref": j("ref"), "junction": j("junction"),
            "is_bridge": any(q["is_bridge"] for q in qs), "bridge_name": bridge_name,
            "is_tunnel": any(q["is_tunnel"] for q in qs), "restricted": any(q["restricted"] for q in qs),
            "river_crossing": any(q["river_crossing"] for q in qs), "osm_way_ids": [str(w) for w in uniq("way_id")],
            "chain": chain}


def travel_graph(rels) -> nx.DiGraph:
    T = nx.DiGraph()
    for r in rels:
        T.add_edge(r["u"], r["v"])
        if not r["oneway"]:
            T.add_edge(r["v"], r["u"])
    return T


def area_polygon(cfg: dict, data: osm_load.OsmData):
    """Config `area: {osm_admin: {name, admin_level}}` -> (Multi)Polygon (lon/lat) or None."""
    spec = (cfg.get("area") or {}).get("osm_admin")
    if not spec:
        return None
    lvl = str(spec["admin_level"])
    hit = [gs for (lv, n), gs in sorted(data.admin.items()) if lv == lvl and Z.norm(n) == Z.norm(spec["name"])]
    if not hit:
        have = sorted(n for lv, n in data.admin if lv == lvl)
        raise SystemExit(f"area {spec['name']!r} (admin_level {lvl}) not in PBF; available: {have}")
    return Z.unique(hit[0], spec["name"], lvl)


def truncate_ways(data: osm_load.OsmData, inside: set[int], by_edge: bool) -> None:
    """OSMnx truncate_graph_polygon on the unsimplified graph: keep nodes inside (+ their way neighbours if
    by_edge) and cut every way into runs of consecutive kept nodes."""
    keep = set(inside)
    if by_edge:
        for _, refs, _ in data.ways:
            for a, b in zip(refs, refs[1:]):
                if a in inside:
                    keep.add(b)
                if b in inside:
                    keep.add(a)
    ways = []
    for wid, refs, tags in data.ways:
        run = []
        for r in refs + [None]:
            if r in keep:
                run.append(r)
                continue
            if len(run) >= 2:
                ways.append((wid, run, tags))
            run = []
    data.ways = ways


def build(cfg: dict, root: Path, out: Path, pbf: Path) -> dict:
    bbox = cfg["bbox"]
    river_cfg = cfg.get("river") or {}
    clip = cfg.get("clip", "both")
    if clip not in ("both", "any"):
        raise SystemExit(f"clip: {clip!r} (use both or any)")
    osmnx = bool(cfg.get("osmnx_compat"))
    network = cfg.get("network_type", "drive")
    levels = {str(z["admin_level"]) for z in cfg["zones"] if z.get("type", z.get("source")) in ("osm_admin", "osm_admin_auto")}
    if cfg.get("area"):
        levels.add(str(cfg["area"]["osm_admin"]["admin_level"]))
    data = osm_load.load(pbf, bbox, f"{network}_osmnx" if osmnx and network == "drive" else network,
                         river_cfg.get("name"), levels, set(cfg.get("include_names") or []))
    epsg = utm_epsg(bbox)
    tm = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True)
    inv = Transformer.from_crs(f"EPSG:{epsg}", "EPSG:4326", always_xy=True)
    to_m = lambda g: shapely.transform(g, tm.transform, interleaved=False)  # noqa: E731
    to_ll = lambda g: shapely.transform(g, inv.transform, interleaved=False)  # noqa: E731

    # ---- study area (admin polygon): load window = area + 500 m, truncated like OSMnx before simplifying
    area_ll = area_polygon(cfg, data)
    area = None
    if area_ll is not None:
        area = to_m(area_ll)
        buf = area.buffer(AREA_BUFFER_M)
        need = to_ll(buf).bounds
        if not (bbox[0] <= need[0] and bbox[1] <= need[1] and need[2] <= bbox[2] and need[3] <= bbox[3]):
            raise SystemExit(f"bbox {bbox} must contain the area + {AREA_BUFFER_M:.0f} m: "
                             f"[{', '.join(f'{v:.3f}' for v in need)}]")
        shapely.prepare(buf)
        ids = list(data.coords)
        bx, by = tm.transform([data.coords[i][0] for i in ids], [data.coords[i][1] for i in ids])
        truncate_ways(data, {i for i, h in zip(ids, shapely.intersects_xy(buf, bx, by)) if h}, clip == "any")
        shapely.prepare(area)
    parts = pieces(data, network, osmnx)
    for p, g in zip(parts, to_m(np.array([LineString(p["coords"]) for p in parts]))):
        p["geom_m"] = g

    # ---- river polygon and river crossings (per piece, before merging)
    river = centerline = None
    if river_cfg:
        river, centerline = rings.river_geometry(data, river_cfg["name"], bbox, to_m)
        for p in parts:
            p["river_crossing"] = p["is_bridge"] and river.intersects(p["geom_m"])
    segs = simplify(parts, cfg.get("keep_way_endpoints", False), osmnx)
    node_ids = sorted({n for s in segs for n in (s["u"], s["v"])})
    xs, ys = tm.transform([data.coords[n][0] for n in node_ids], [data.coords[n][1] for n in node_ids])
    xy = {n: (x, y) for n, x, y in zip(node_ids, xs, ys)}

    # ---- zones and node assignment (first zone in inside->outside order wins)
    ctx = Z.Ctx(segs, xy, river, centerline, data.admin, to_m, root, area)
    zones, ring_info = Z.build(ctx, cfg["zones"])
    order = {z["zone"]: z["zone_order"] for z in zones}
    pts = shapely.points([xy[n] for n in node_ids])
    zone_of: dict[int, str] = {}
    for z in zones:
        for n, hit in zip(node_ids, shapely.intersects(z["geom"], pts)):
            if hit and n not in zone_of:
                zone_of[n] = z["zone"]
    if river is not None:  # bridge nodes over the water: nearest zone
        for s in segs:
            for n in (s["u"], s["v"]):
                if s["river_crossing"] and n not in zone_of and river.intersects(Point(xy[n])):
                    d, _, zone = min((z["geom"].distance(Point(xy[n])), z["zone_order"], z["zone"]) for z in zones)
                    if d <= BRIDGE_NODE_MAX_M:
                        zone_of[n] = zone

    # ---- street_count on the unclipped network, clip, keep the largest connected component
    street_count = Counter()
    for s in segs:
        street_count[s["u"]] += 1
        street_count[s["v"]] += 1
    # clip both: both ends inside; any: OSMnx truncate_by_edge (nodes inside + their neighbours, all edges among them)
    if area is None:
        inside = set(zone_of)
    else:
        inside = {n for n, hit in zip(node_ids, shapely.intersects(area, pts)) if hit}
    keep = set(inside)
    if clip == "any":
        for s in segs:
            if s["u"] in inside:
                keep.add(s["v"])
            if s["v"] in inside:
                keep.add(s["u"])
    kept = [s for s in segs if s["u"] in keep and s["v"] in keep]
    unzoned = sorted({n for s in kept for n in (s["u"], s["v"])} - zone_of.keys())
    if unzoned:  # outside every zone (gaps, water, just outside the area): nearest zone, inner zone on ties
        tree = shapely.STRtree([z["geom"] for z in zones])
        for n in unzoned:
            zone_of[n] = min((zones[i]["zone_order"], zones[i]["zone"])
                             for i in tree.query_nearest(Point(xy[n]), all_matches=True))[1]
    comps = sorted(nx.weakly_connected_components(travel_graph(kept)), key=lambda c: (-len(c), min(c)))
    keep_nodes = comps[0] if comps else set()
    dropped = [{"size": len(c), "nodes": sorted(c), "zones": sorted({zone_of[n] for n in c}),
                "street_names": sorted({nm for s in kept if s["u"] in c for nm in s["names"]})[:6]} for c in comps[1:]]
    kept = [s for s in kept if s["u"] in keep_nodes]

    # ---- records
    ring_of = {sid: info["label"] for info in ring_info.values() if info["label"] for sid in info["seg_ids"]}
    bridge_zone = river_cfg.get("bridge_zone")
    rels = []
    for s in sorted(kept, key=lambda s: (s["way_id"], int(s["id"].split("-")[1]))):
        zone = bridge_zone if (bridge_zone and s["river_crossing"]) else max(
            (zone_of[s["u"]], zone_of[s["v"]]), key=order.get)
        rels.append({**{k: s[k] for k in export_neo4j.REL_FIELDS if k in s}, "start_id": s["u"], "end_id": s["v"],
                     "u": s["u"], "v": s["v"], "ring": ring_of.get(s["id"]), "zone": zone,
                     "osm_way_ids": s["osm_way_ids"],
                     "geometry_wkt": shapely.to_wkt(LineString(s["coords"]), rounding_precision=7, trim=True)})
    incident = defaultdict(list)
    for r in rels:
        incident[r["u"]].append(r)
        if r["v"] != r["u"]:
            incident[r["v"]].append(r)
    nodes = []
    for n in sorted(keep_nodes):
        inc = incident[n]
        kind = ("bridge_end" if any(r["river_crossing"] for r in inc)
                else "dead_end" if street_count[n] == 1 else "intersection")
        lon, lat = data.coords[n]
        nodes.append({"id": n, "lat": round(lat, 7), "lon": round(lon, 7), "zone": zone_of[n],
                      "zone_order": order[zone_of[n]], "street_count": street_count[n],
                      "degree": sum((r["u"] == n) + (r["v"] == n) for r in inc), "kind": kind,
                      "traffic_signals": n in data.signals,
                      "street_names": sorted({nm for r in inc for nm in r["names"]})})

    stats = make_stats(cfg, pbf, data, nodes, rels, zones, ring_info, dropped, river, centerline, xy)
    have = stats["input"]["bbox"]
    if have and not (have[0] <= bbox[0] and have[1] <= bbox[1] and bbox[2] <= have[2] and bbox[3] <= have[3]):
        stats["warnings"].append(f"the PBF covers {have}, less than the config bbox {bbox}")
    G = None
    if area is not None:
        spec = cfg["area"]["osm_admin"]
        stats["area"] = {"name": spec["name"], "admin_level": spec["admin_level"], "km2": round(area.area / 1e6, 2),
                         "clip": clip, "load_buffer_m": AREA_BUFFER_M}
    if osmnx:
        sc = export_osmnx.street_counts(segs, keep_nodes)
        G = export_osmnx.graph(sorted(kept, key=lambda s: (s["way_id"], int(s["id"].split("-")[1]))), sc, data,
                               stats["input"]["osm_timestamp"])
        stats["osmnx"] = export_osmnx.summary(G)
    export_neo4j.write_all(out, cfg, nodes, rels, zones, river, centerline, to_ll, stats,
                           area_ll)
    if G is not None:
        export_osmnx.write(G, out / "osmnx.graphml")
    return stats


# ---------------------------------------------------------------- stats & checks
def make_stats(cfg, pbf, data, nodes, rels, zones, ring_info, dropped, river, centerline, xy) -> dict:
    checks = cfg.get("checks") or {}
    node_by_id = {n["id"]: n for n in nodes}
    zone_of = {n["id"]: n["zone"] for n in nodes}
    zs = {}
    for z in [z["zone"] for z in zones] + ([cfg["river"]["bridge_zone"]] if (cfg.get("river") or {}).get("bridge_zone") else []):
        rs = [r for r in rels if r["zone"] == z]
        zs[z] = {"nodes": sum(1 for n in nodes if n["zone"] == z), "segments": len(rs),
                 "km": round(sum(r["length_m"] for r in rs) / 1000, 2)}
        zs[z].update({"zone_name": x["name"] for x in zones if x["zone"] == z and "name" in x})
    bridges = rings.bridge_checks(checks.get("bridges", []), rels, centerline, xy, zone_of, travel_graph)
    crossings = Counter(r["bridge_name"] for r in rels if r["river_crossing"])
    ring_stats, boundaries = rings.ring_checks(ring_info, rels, xy, travel_graph)

    streets = {}
    for street, allowed in (checks.get("streets_in_zones") or {}).items():
        found = Counter(n["zone"] for n in nodes if street in n["street_names"])
        streets[street] = {"expected": allowed, "found": dict(sorted(found.items())),
                           "ok": bool(found) and set(found) <= set(allowed)}

    T = travel_graph(rels)
    scc = sorted((len(c) for c in nx.strongly_connected_components(T)), reverse=True)
    ids = set(node_by_id)
    def ends_match(r):
        c = shapely.from_wkt(r["geometry_wkt"]).coords
        a, b = node_by_id.get(r["u"]), node_by_id.get(r["v"])
        return bool(a and b) and all(abs(p - q) < 1e-7 for p, q in zip(c[0] + c[-1], (a["lon"], a["lat"], b["lon"], b["lat"])))
    wkt_ok = all(ends_match(r) for r in rels)
    sanity = {"endpoints_exist": all(r["u"] in ids and r["v"] in ids for r in rels),
              "wkt_endpoints_match_nodes": wkt_ok, "lengths_positive": all(r["length_m"] > 0 for r in rels),
              "unique_segment_ids": len({r["id"] for r in rels}) == len(rels),
              "no_isolated_nodes": all(n["degree"] > 0 for n in nodes)}
    summary = {"sanity": all(sanity.values()),
               "bridges": all(b["ok"] for b in bridges.values()),
               "rings_continuous": all(r["continuous_undirected"] for r in ring_stats.values()),
               "streets_in_zones": all(s["ok"] for s in streets.values())}
    n_drop = sum(d["size"] for d in dropped)
    share = round(n_drop / (n_drop + len(nodes)), 4) if nodes else 0.0
    hdr = osm_load.header(pbf)
    return {
        "city": cfg.get("name"),
        "input": {**hdr, "sha256": osm_load.sha256(pbf), "network_type": cfg.get("network_type", "drive"),
                  "load_bbox": cfg["bbox"], "license": "(c) OpenStreetMap contributors, ODbL 1.0",
                  **({"source": json.loads(src.read_text(encoding="utf-8"))}
                     if (src := fetch.source_path(pbf)).is_file() else {})},
        "versions": {"python": platform.python_version(), "geos": shapely.geos_version_string,
                     **{p: version(p) for p in ("citygraph", "osmium", "shapely", "networkx", "pyproj")}},
        "zones": zs,
        "totals": {"nodes": len(nodes), "segments": len(rels), "km": round(sum(r["length_m"] for r in rels) / 1000, 2),
                   "kinds": dict(sorted(Counter(n["kind"] for n in nodes).items())),
                   "oneway_segments": sum(r["oneway"] for r in rels),
                   "unnamed_segments": sum(1 for r in rels if not r["name"]),
                   "self_loops": sum(1 for r in rels if r["u"] == r["v"]),
                   "restricted_segments": sum(r["restricted"] for r in rels),
                   "traffic_signal_nodes": sum(n["traffic_signals"] for n in nodes),
                   "nodes_degree_ne_street_count": sum(1 for n in nodes if n["degree"] != n["street_count"])},
        "river": None if river is None else {
            "name": cfg["river"]["name"], "polygon_km2_in_window": round(river.area / 1e6, 2),
            "river_crossing_segments": sum(crossings.values()), "crossing_bridges": dict(sorted(
                ((k or "(unnamed)"), v) for k, v in crossings.items()))},
        "bridges": bridges,
        "rings": ring_stats,
        "boundaries": boundaries,
        "streets_in_zones": streets,
        "connectivity": {"strongly_connected_components": len(scc),
                         "largest_scc_nodes": scc[0] if scc else 0,
                         "largest_scc_share": round(scc[0] / len(nodes), 4) if scc else 0.0,
                         "note": "travel direction: two-way segments traversable both ways"},
        "dropped_fragments": {"note": "weakly connected parts outside the largest one", "count": len(dropped),
                              "nodes_dropped": n_drop, "share": share, "fragments": dropped},
        "warnings": [f"dropped {n_drop} nodes ({share:.1%}) outside the largest connected component"]
        if share > MAX_DROPPED_SHARE else [],
        "sanity": sanity,
        "checks": summary,
    }

