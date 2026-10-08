"""out/<city>/osmnx.graphml: the graph as OSMnx 2.1.1 would build it with graph_from_polygon(simplify=True,
truncate_by_edge=...) and write it with save_graphml (MultiDiGraph, every value stored as str()).
Load it with `G = ox.load_graphml(path)`."""
from __future__ import annotations

from collections import Counter
from importlib.metadata import version
from pathlib import Path

import networkx as nx
import numpy as np
import shapely
from shapely.geometry import LineString

from .osm_load import NODE_TAGS

EARTH_RADIUS_M = 6_371_009  # OSMnx great-circle radius
WAY_ATTRS = ("access", "area", "bridge", "est_width", "highway", "junction", "landuse", "lanes", "maxspeed", "name",
             "ref", "service", "tunnel", "width")  # OSMnx useful_tags_way without oneway (that becomes a bool)


def great_circle(c: np.ndarray) -> np.ndarray:
    """c: [[lon, lat], ...] -> metres between consecutive points (haversine, as OSMnx add_edge_lengths)."""
    y1, y2 = np.deg2rad(c[:-1, 1]), np.deg2rad(c[1:, 1])
    dx = np.deg2rad(c[1:, 0]) - np.deg2rad(c[:-1, 0])
    h = np.minimum(1, np.sin((y2 - y1) / 2) ** 2 + np.cos(y1) * np.cos(y2) * np.sin(dx / 2) ** 2)
    return 2 * np.arcsin(np.sqrt(h)) * EARTH_RADIUS_M


def edge_attrs(steps: list[tuple[dict, bool]]) -> dict:
    """steps: [(piece, walked along the piece)] in travel order. A piece is one or more OSM node-to-node edges.
    One OSM edge -> its own attributes; several -> merged like OSMnx simplify_graph (length summed, equal values
    kept, differing values -> list(set(values)) in travel order. That order is deterministic for ints/bools (osmid,
    reversed) and matches OSMnx; string lists are sorted here (OSMnx's order depends on PYTHONHASHSEED)."""
    edges = []  # one attribute dict per OSM edge
    for q, f in steps:
        base = {"osmid": q["way_id"], **{k: q["tags"][k] for k in WAY_ATTRS if k in q["tags"]},
                "oneway": q["oneway"], "reversed": not f}
        lengths = q["gc"] if f else q["gc"][::-1]
        edges += [{**base, "length": float(x)} for x in lengths]
    if len(edges) == 1:
        return edges[0]
    out = {}
    for k in dict.fromkeys(k for e in edges for k in e):
        vals = [e[k] for e in edges if k in e]
        u = set(vals)
        out[k] = (sum(vals) if k == "length" else vals[0] if len(u) == 1
                  else sorted(u) if isinstance(vals[0], str) else list(u))
    coords = []
    for q, f in steps:
        c = q["coords"] if f else q["coords"][::-1]
        coords += c if not coords else c[1:]
    out["geometry"] = LineString(coords)
    return out


def street_counts(segs: list[dict], nodes) -> dict:
    """OSMnx count_streets_per_node: undirected view of the directed multigraph, where u->v and v->u edges with
    the same key collapse (so a pair of opposite one-way segments counts once); any self-loops count 2."""
    directed, loops = Counter(), set()
    for s in segs:
        directed[s["u"], s["v"]] += 1
        if not s["oneway"]:
            directed[s["v"], s["u"]] += 1
        if s["u"] == s["v"]:
            loops.add(s["u"])
    sc = Counter()
    for (u, v), c in directed.items():
        if u != v and (c > directed[v, u] or (c == directed[v, u] and u < v)):
            sc[u] += c
            sc[v] += c
    return {n: sc[n] + 2 * (n in loops) for n in nodes}


def graph(segs: list[dict], street_count: dict, data, created: str | None) -> nx.MultiDiGraph:
    for s in segs:
        for q, _ in s["chain"]:
            if "gc" not in q:
                q["gc"] = great_circle(np.array(q["coords"]))
    G = nx.MultiDiGraph(created_date=created, created_with=f"citygraph {version('citygraph')} (OSMnx 2.1.1 layout)",
                        crs="epsg:4326", simplified=True)
    for n in sorted(street_count):
        lon, lat = data.coords[n]
        G.add_node(n, y=lat, x=lon, **{k: data.node_tags[n][k] for k in NODE_TAGS if k in data.node_tags.get(n, {})},
                   street_count=street_count[n])
    for s in segs:  # two-way segments: both directions; the reverse one has reversed=True
        G.add_edge(s["u"], s["v"], **edge_attrs(s["chain"]))
        if not s["oneway"]:
            G.add_edge(s["v"], s["u"], **edge_attrs([(q, not f) for q, f in reversed(s["chain"])]))
    return G


def summary(G: nx.MultiDiGraph) -> dict:
    """Superblocker's graph_to_street_network counts: physical segment = (min(u,v), max(u,v), first osmid).
    A merged two-way street whose osmid list comes out in a different order per direction counts twice."""
    phys = {}
    for u, v, d in G.edges(data=True):
        osmid = d["osmid"][0] if isinstance(d["osmid"], list) else d["osmid"]
        phys.setdefault((min(u, v), max(u, v), osmid), round(d["length"], 2))
    return {"nodes": len(G), "directed_edges": G.number_of_edges(), "physical_segments": len(phys),
            "km": round(sum(phys.values()) / 1000, 2), "note": "Superblocker graph_to_street_network definitions"}


def write(G: nx.MultiDiGraph, path: Path) -> None:
    H = nx.MultiDiGraph(**{k: str(v) for k, v in G.graph.items()})
    H.add_nodes_from((n, {k: str(v) for k, v in d.items()}) for n, d in G.nodes(data=True))
    H.add_edges_from((u, v, k, {a: shapely.to_wkt(x, rounding_precision=-1) if a == "geometry" else str(x)
                                for a, x in d.items()}) for u, v, k, d in G.edges(keys=True, data=True))
    nx.write_graphml(H, path, encoding="utf-8")
