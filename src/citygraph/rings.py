"""River, bridges and `ring` zones (boundary road between two bridge heads, closed along the river),
plus their checks. Only used when a config has a `river:` block or `type: ring` zones."""
from __future__ import annotations

import math

import networkx as nx
import shapely
from shapely.geometry import LineString, Point, Polygon, box
from shapely.ops import linemerge, substring, unary_union

RING_BUFFER_M = 15.0    # boundary roads count as inside (both carriageways -> inner zone)
PARALLEL_M = 60.0       # carriageways named like the ring this close to the chain are boundary roads
CONNECTOR_M = 25.0      # same, for other street names the chain itself uses (squares, links)
OFF_NAME_PENALTY = 8.0  # routing penalty for edges without a boundary street name
HEAD_RADIUS_M = 150.0   # "drivable" ring checks start/end at ring nodes this close to the bridge heads


# ---------------------------------------------------------------- river
def centerline(lines: list[LineString], window=None) -> LineString | None:
    """Merge waterway=river ways into one line: shortest path between the two most distant open ends
    (drops parallel arms around islands). If the ways do not join up (a stretch mapped under another name, a lake
    in between), only the connected part with the most length inside `window` is used."""
    merged = linemerge(lines) if lines else None
    if merged is None or merged.is_empty:
        return None
    parts = list(getattr(merged, "geoms", [merged]))
    G = nx.Graph()
    for i, p in enumerate(parts):
        a, b = p.coords[0], p.coords[-1]
        if not G.has_edge(a, b) or G[a][b]["len"] > p.length:
            G.add_edge(a, b, len=p.length, i=i)
    comps = list(nx.connected_components(G))
    if len(comps) > 1:
        size = lambda c: sum((p if window is None else p.intersection(window)).length  # noqa: E731
                             for p in parts if p.coords[0] in c)
        comps.sort(key=lambda c: (-size(c), min(c)))
    ends = sorted(n for n in comps[0] if G.degree(n) == 1)
    if len(ends) < 2:
        return max((p for p in parts if p.coords[0] in comps[0]), key=lambda p: p.length)
    a, b = max(((x, y) for x in ends for y in ends if x < y), key=lambda xy: math.dist(*xy))
    path = nx.shortest_path(G, a, b, weight="len")
    coords = [path[0]]
    for x, y in zip(path, path[1:]):
        c = list(parts[G[x][y]["i"]].coords)
        coords += (c if c[0] == x else c[::-1])[1:]
    return LineString(coords)


def river_geometry(data, name: str | list[str], bbox, to_m) -> tuple:
    """-> (river polygon, centerline), metric. Polygon = water areas touching the named centerline."""
    cl = centerline(data.river_lines, box(*bbox))
    if cl is None:
        raise SystemExit(f"river {name!r}: no waterway=river way with that name in the PBF")
    frame = to_m(box(*bbox)).buffer(2000)
    river = to_m(unary_union([a for a in data.water_areas if a.intersects(cl)])).intersection(frame)
    shapely.prepare(river)
    return river, to_m(cl)


# ---------------------------------------------------------------- ring zones
def bridge_head(ctx, bridge: str, bank: str) -> int:
    nodes = sorted({n for s in ctx.segs if s["is_bridge"] and s["bridge_name"] == bridge for n in (s["u"], s["v"])})
    if not nodes:
        raise SystemExit(f"bridge not found in data: {bridge!r}")
    axis, sign = {"east": (0, 1), "west": (0, -1), "north": (1, 1), "south": (1, -1)}[bank]
    return max(nodes, key=lambda n: sign * ctx.xy[n][axis])


def anchor_node(ctx, a: str, b: str) -> int:
    cand = [n for n, nm in ctx.names.items() if {a, b} <= nm]
    if not cand:
        raise SystemExit(f"anchor intersection not found: {a} x {b}")
    return min(cand)


def ring_zone(ctx, spec: dict) -> tuple:
    """Chain of boundary roads `from` bridge head -> `via` anchors -> `to` bridge head (shortest path preferring
    `streets` names, river crossings excluded), closed along the river centerline, river cut out."""
    names = set(spec["streets"])
    R = nx.Graph()
    for s in ctx.segs:
        if not s["river_crossing"]:
            w = s["length_m"] * (1.0 if names & set(s["names"]) else OFF_NAME_PENALTY)
            if not R.has_edge(s["u"], s["v"]) or R[s["u"]][s["v"]]["w"] > w:
                R.add_edge(s["u"], s["v"], w=w, seg=s)
    stops = ([bridge_head(ctx, spec["from"], spec["bank"])] + [anchor_node(ctx, a, b) for a, b in spec.get("via", [])]
             + [bridge_head(ctx, spec["to"], spec["bank"])])
    nodes = [stops[0]]
    for a, b in zip(stops, stops[1:]):
        nodes += nx.shortest_path(R, a, b, weight="w")[1:]
    chain_segs = [R[a][b]["seg"] for a, b in zip(nodes, nodes[1:])]
    coords = [c for a, s in zip(nodes, chain_segs)
              for c in (list(s["geom_m"].coords) if s["u"] == a else list(s["geom_m"].coords)[::-1])]
    chain = LineString(coords)
    closure = []
    if ctx.centerline is not None:  # back along the river, from the end bridge to the start bridge
        cl = ctx.centerline
        closure = list(substring(cl, cl.project(Point(coords[-1])), cl.project(Point(coords[0]))).coords)
    poly = shapely.make_valid(Polygon(coords + closure))
    land = unary_union([g for g in getattr(poly, "geoms", [poly]) if g.geom_type in ("Polygon", "MultiPolygon")])
    if ctx.river is not None:
        land = land.difference(ctx.river)
    land = unary_union([p for p in getattr(land, "geoms", [land]) if p.intersects(chain.buffer(1.0))])
    # boundary roads: the chain plus parallel carriageways / connectors close to it
    chain_ids = {s["id"] for s in chain_segs}
    used = {nm for s in chain_segs for nm in s["names"]} - names
    bsegs = []
    for s in ctx.segs:
        if s["river_crossing"]:
            continue
        nms = set(s["names"])
        tol = PARALLEL_M if nms & names else CONNECTOR_M if nms & used else None
        if s["id"] in chain_ids or (tol and chain.distance(Point(ctx.xy[s["u"]])) <= tol
                                    and chain.distance(Point(ctx.xy[s["v"]])) <= tol):
            bsegs.append(s)
    geom = unary_union([land, unary_union([s["geom_m"] for s in bsegs]).buffer(RING_BUFFER_M)])
    info = {"label": spec.get("ring"), "start": stops[0], "end": stops[-1],
            "seg_ids": sorted(s["id"] for s in bsegs),
            "street_sequence": list(dict.fromkeys(nm for s in chain_segs for nm in s["names"]))}
    return geom, info


# ---------------------------------------------------------------- checks
def side_of(line: LineString, p) -> int:
    """+1 / -1: which side of the (directed) river centerline a point lies on."""
    d = line.project(Point(p))
    a, b = line.interpolate(max(d - 5, 0)), line.interpolate(min(d + 5, line.length))
    return 1 if (b.x - a.x) * (p[1] - a.y) - (b.y - a.y) * (p[0] - a.x) > 0 else -1


def reach(G, a_nodes, b_nodes) -> bool:
    return any(nx.has_path(G, a, b) for a in a_nodes for b in b_nodes)


def bridge_checks(names, rels, centerline, xy, zone_of, travel_graph) -> dict:
    """Each bridge: present, crosses the river, connects both sides of the centerline in both directions."""
    out = {}
    for name in names:
        rs = [r for r in rels if r["bridge_name"] == name]
        B = travel_graph(rs)
        sides = {n: side_of(centerline, xy[n]) for n in B} if centerline is not None else {}
        L, R = [n for n in B if sides.get(n) == 1], [n for n in B if sides.get(n) == -1]
        res = {"segments": len(rs), "length_m": round(sum(r["length_m"] for r in rs), 1),
               "river_crossing": any(r["river_crossing"] for r in rs),
               "endpoint_zones": sorted({zone_of[n] for n in B}),
               "left_to_right": reach(B, L, R), "right_to_left": reach(B, R, L)}
        res["ok"] = bool(rs) and res["river_crossing"] and res["left_to_right"] and res["right_to_left"]
        out[name] = res
    return out


def ring_checks(rings, rels, xy, travel_graph) -> tuple[dict, dict]:
    """-> (labelled rings: continuity river-to-river + drivability, unlabelled boundaries: street sequence)."""
    stats = {}
    for zone, info in rings.items():
        if not info["label"]:
            continue
        rs = [r for r in rels if r["ring"] == info["label"]]
        R = travel_graph(rs)
        s, e = info["start"], info["end"]
        near = lambda h: [n for n in R if math.dist(xy[n], xy[h]) <= HEAD_RADIUS_M]  # noqa: E731
        stats[info["label"]] = {
            "zone": zone, "segments": len(rs), "km": round(sum(r["length_m"] for r in rs) / 1000, 2),
            "start_node": s, "end_node": e,
            "continuous_undirected": s in R and e in R and nx.has_path(R.to_undirected(), s, e),
            "drivable_start_to_end": reach(R, near(s), near(e)),
            "drivable_end_to_start": reach(R, near(e), near(s)),
            "chain_street_sequence": info["street_sequence"]}
    boundaries = {z: {"start_node": i["start"], "end_node": i["end"], "chain_street_sequence": i["street_sequence"]}
                  for z, i in rings.items() if not i["label"]}
    return stats, boundaries
