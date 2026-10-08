"""Write out/<city>/: neo4j/ (CSV + import.cypher + sample_queries.cypher), graph.graphml, zones.geojson, stats.json."""
from __future__ import annotations

import csv
import json
from importlib.resources import files
from pathlib import Path

import networkx as nx
import shapely
from shapely.geometry import mapping

REL_FIELDS = ["id", "name", "names", "highway", "length_m", "oneway", "lanes", "maxspeed", "ref", "junction",
              "is_bridge", "river_crossing", "bridge_name", "is_tunnel", "restricted"]
NODE_COLS = ["id", "lat", "lon", "zone", "zone_order", "street_count", "degree", "kind", "traffic_signals",
             "street_names"]
REL_COLS = ["id", "start_id", "end_id"] + REL_FIELDS[1:] + ["ring", "zone", "osm_way_ids", "geometry_wkt"]


def write_text(path: Path, text: str) -> None:
    """UTF-8 with LF line endings on every platform (byte-identical outputs)."""
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def cell(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, list):  # ';' separates list items, so it must not occur inside one
        return ";".join(str(x).replace(";", ",") for x in v)
    return v


def write_csv(path: Path, header, rows) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(header)
        w.writerows(rows)


def cypher_literal(v) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, list):
        return "[" + ", ".join(cypher_literal(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{" + ", ".join(f"{k}: {cypher_literal(x)}" for k, x in v.items()) + "}"
    return "'" + str(v).replace("\\", "\\\\").replace("'", "\\'") + "'"


def write_neo4j(d: Path, cfg: dict, nodes, rels) -> None:
    d.mkdir(parents=True, exist_ok=True)
    write_csv(d / "intersections.csv", NODE_COLS, [[cell(n[c]) for c in NODE_COLS] for n in nodes])
    write_csv(d / "streets.csv", REL_COLS, [[cell(r[c]) for c in REL_COLS] for r in rels])
    src = files("citygraph") / "cypher"
    write_text(d / "import.cypher", src.joinpath("import.cypher").read_text(encoding="utf-8"))
    head = (f"// Per-city parameters for {cfg.get('name')} (from the city config `neo4j_params`).\n"
            "// Paste this file into Neo4j Browser (or run it with cypher-shell -f).\n")
    head += "".join(f":param {k} => {cypher_literal(v)};\n" for k, v in (cfg.get("neo4j_params") or {}).items())
    write_text(d / "sample_queries.cypher", head + "\n" + src.joinpath("sample_queries.cypher").read_text(encoding="utf-8"))


def write_graphml(path: Path, cfg: dict, nodes, rels, stats) -> None:
    X = nx.MultiDiGraph(name=f"{cfg.get('name')} street graph", crs="EPSG:4326",
                        source=f"OpenStreetMap {stats['input']['osm_timestamp']}, (c) OpenStreetMap contributors, ODbL")
    for n in nodes:
        X.add_node(str(n["id"]), **{k: cell(v) if isinstance(v, list) else v for k, v in n.items() if k != "id"})
    for r in rels:
        X.add_edge(str(r["start_id"]), str(r["end_id"]), key=r["id"],
                   **{k: cell(r[k]) if isinstance(r[k], list) else r[k]
                      for k in REL_COLS if k not in ("start_id", "end_id") and r[k] is not None})
    nx.write_graphml(X, path)


def ll(geom, to_ll):
    return mapping(shapely.make_valid(shapely.set_precision(to_ll(geom), 1e-7)))


def write_zones(path: Path, zones, river, centerline, to_ll, area_ll=None, other_bridges=()) -> None:
    feats = [{"type": "Feature", "properties": {"zone": z["zone"], "zone_order": z["zone_order"], "source": z["source"],
                                                **({"zone_name": z["name"]} if "name" in z else {})},
              "geometry": ll(z["geom"], to_ll)} for z in zones]
    if area_ll is not None:  # the study area (admin boundary), exactly as used for clipping
        feats.append({"type": "Feature", "properties": {"kind": "boundary"}, "geometry": mapping(area_ll)})
    if river is not None:
        frame = shapely.box(*shapely.unary_union([z["geom"] for z in zones]).buffer(500).bounds)
        feats.append({"type": "Feature", "properties": {"kind": "river"}, "geometry": ll(river.intersection(frame), to_ll)})
        feats.append({"type": "Feature", "properties": {"kind": "river_centerline"},
                      "geometry": ll(centerline.intersection(frame), to_ll)})
    for b in other_bridges:  # river bridges that are not part of the graph (railways, filtered-out roads)
        feats.append({"type": "Feature", "properties": {"kind": "bridge_not_in_graph", "name": b["name"],
                                                        "bridge": b["kind"]}, "geometry": ll(b["geom"], to_ll)})
    write_text(path, json.dumps({"type": "FeatureCollection", "features": feats}, ensure_ascii=False))


def write_all(out: Path, cfg, nodes, rels, zones, river, centerline, to_ll, stats, area_ll=None,
              other_bridges=()) -> None:
    out.mkdir(parents=True, exist_ok=True)
    write_neo4j(out / "neo4j", cfg, nodes, rels)
    write_graphml(out / "graph.graphml", cfg, nodes, rels, stats)
    write_zones(out / "zones.geojson", zones, river, centerline, to_ll, area_ll, other_bridges)
    write_text(out / "stats.json", json.dumps(stats, ensure_ascii=False, indent=2) + "\n")
