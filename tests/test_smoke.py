"""Fast checks on a synthetic OSM file, plus sanity checks on out/budapest when it has been built."""
import csv
import json
from pathlib import Path

import pytest

from citygraph import graph_build, osm_load, render

ROOT = Path(__file__).resolve().parents[1]

# 3x3 grid around (19.000, 47.500); ids r*3+c+1. Drive filter drops the footway; node 11 is a dead end
# reached by a oneway=-1 way; nodes 20-21 are a 2-node fragment; relation 300 is the only zone (and the `area`
# of the whole-city test, split into two admin_level 10 halves 301/302); way 140 leaves it eastwards.
NODES = {r * 3 + c + 1: (19.000 + 0.001 * c, 47.500 + 0.001 * r) for r in range(3) for c in range(3)}
NODES |= {11: (19.003, 47.5025), 20: (19.0015, 47.5015), 21: (19.0018, 47.5017),
          50: (18.9995, 47.4995), 51: (19.0035, 47.4995), 52: (19.0035, 47.5030), 53: (18.9995, 47.5030),
          54: (19.0015, 47.4995), 55: (19.0015, 47.5030), 60: (19.006, 47.500), 61: (19.008, 47.500)}
WAYS = [(100, [1, 2, 3], {"highway": "residential", "name": "A utca"}),
        (101, [4, 5], {"highway": "residential", "name": "B utca"}),
        (102, [5, 6], {"highway": "residential", "name": "B utca"}),
        (103, [7, 8], {"highway": "residential", "name": "C utca"}),
        (104, [8, 9], {"highway": "residential", "name": "C utca"}),
        (110, [1, 4, 7], {"highway": "residential", "name": "D utca"}),
        (111, [3, 6, 9], {"highway": "secondary", "name": "E utca", "oneway": "yes"}),
        (120, [2, 5], {"highway": "footway"}),
        (121, [9, 11], {"highway": "residential", "name": "F utca", "oneway": "-1"}),
        (130, [20, 21], {"highway": "residential", "name": "G utca"}),
        (140, [3, 60, 61], {"highway": "residential", "name": "H utca"}),
        (200, [50, 51, 52, 53, 50], {}), (201, [50, 54, 55, 53, 50], {}), (202, [54, 51, 52, 55, 54], {})]
RELS = [(300, 200, "9", "Test district"), (301, 201, "10", "Nyugat"), (302, 202, "10", "Kelet-Dél")]


def write_osm(path: Path, nodes: dict = NODES, ways: list = WAYS) -> None:
    tag = lambda t: "".join(f'<tag k="{k}" v="{v}"/>' for k, v in t.items())  # noqa: E731
    xml = ['<?xml version="1.0" encoding="UTF-8"?>', '<osm version="0.6" generator="test">']
    xml += [f'<node id="{i}" version="1" lat="{la}" lon="{lo}"/>' for i, (lo, la) in sorted(nodes.items())]
    xml += [f'<way id="{w}" version="1">' + "".join(f'<nd ref="{n}"/>' for n in refs) + tag(t) + "</way>"
            for w, refs, t in ways]
    xml += [f'<relation id="{r}" version="1"><member type="way" ref="{w}" role="outer"/>'
            + tag({"type": "boundary", "boundary": "administrative", "admin_level": lvl, "name": name}) + "</relation>"
            for r, w, lvl, name in RELS] + ["</osm>"]
    path.write_text("\n".join(xml), encoding="utf-8")


CFG = {"name": "Test", "bbox": [18.99, 47.49, 19.01, 47.51], "network_type": "drive",
       "zones": [{"type": "osm_admin", "admin_level": 9, "areas": {"T": ["Nope", "TEST  district"]}}],  # loose name match
       "neo4j_params": {"ring": None}}


def read(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("city")
    write_osm(tmp / "test.osm")
    stats = graph_build.build(CFG, tmp, tmp / "out", tmp / "test.osm")
    return tmp, stats


def test_topology(built):
    tmp, stats = built
    rels = {r["id"]: r for r in read(tmp / "out/neo4j/streets.csv")}
    nodes = {n["id"]: n for n in read(tmp / "out/neo4j/intersections.csv")}
    assert set(rels) == {"100-0", "101-0", "103-0", "111-0", "111-1", "121-0"}   # footway + fragment gone
    assert set(nodes) == {"3", "4", "6", "9", "11"}                               # pass-through nodes merged
    assert (rels["121-0"]["start_id"], rels["121-0"]["end_id"], rels["121-0"]["oneway"]) == ("11", "9", "true")
    r = rels["100-0"]                                                             # 4 -> 1 -> 3 through corner 1
    assert (r["start_id"], r["end_id"], r["names"], r["name"], r["osm_way_ids"]) == ("4", "3", "D utca;A utca", "A utca", "110;100")
    assert nodes["11"]["kind"] == "dead_end" and nodes["9"]["street_count"] == "3"
    assert {n["zone"] for n in nodes.values()} == {"T"}
    assert stats["dropped_fragments"]["fragments"][0]["size"] == 2 and all(stats["checks"].values())


def test_deterministic_and_replayable(built, tmp_path):
    tmp, _ = built
    graph_build.build(CFG, tmp, tmp_path / "again", tmp / "test.osm")
    for f in ("neo4j/streets.csv", "neo4j/intersections.csv", "zones.geojson", "stats.json", "graph.graphml"):
        assert (tmp / "out" / f).read_bytes() == (tmp_path / "again" / f).read_bytes(), f
    replay = {**CFG, "zones": [{"type": "geojson", "file": "out/zones.geojson"}]}
    graph_build.build(replay, tmp, tmp_path / "replay", tmp / "test.osm")
    assert read(tmp / "out/neo4j/intersections.csv") == read(tmp_path / "replay/neo4j/intersections.csv")


def test_render_and_clip(built, tmp_path):
    tmp, _ = built
    assert all(p.stat().st_size > 0 for p in render.render(tmp / "out", "Test"))
    osm_load.clip(tmp / "test.osm", tmp_path / "clip.osm.pbf", [18.999, 47.499, 19.0011, 47.5011])  # includes boundary node 50
    d = osm_load.load(tmp_path / "clip.osm.pbf", CFG["bbox"], "drive", None, {"9"})
    assert {w for w, _, _ in d.ways} == {100, 101, 102, 110} and ("9", "Test district") in d.admin


def test_area_auto_zones_and_clip(built, tmp_path):
    tmp, _ = built
    cfg = {**CFG, "bbox": [18.98, 47.48, 19.02, 47.52], "osmnx_compat": True,
           "area": {"osm_admin": {"name": "Test district", "admin_level": 9}},
           "zones": [{"type": "osm_admin_auto", "admin_level": 10, "order_from": [19.003, 47.5025]}]}
    rels = {}
    for clip in ("both", "any"):
        stats = graph_build.build({**cfg, "clip": clip}, tmp, tmp_path / clip, tmp / "test.osm")
        rels[clip] = {r["id"]: r for r in read(tmp_path / clip / "neo4j/streets.csv")}
    assert set(rels["any"]) - set(rels["both"]) == {"140-0"}          # one end inside is enough (truncate_by_edge)
    assert {z: (v["zone_name"], i) for i, (z, v) in enumerate(stats["zones"].items())} == {
        "KELET_DEL": ("Kelet-Dél", 0), "NYUGAT": ("Nyugat", 1)}       # ordered by distance from order_from
    nodes = {n["id"]: n for n in read(tmp_path / "any/neo4j/intersections.csv")}
    assert nodes["61"]["zone"] == "KELET_DEL"                           # outside every zone -> nearest
    ox = stats["osmnx"]
    assert ox["directed_edges"] == 2 * len(rels["any"]) - sum(r["oneway"] == "true" for r in rels["any"].values())
    assert (tmp_path / "any/osmnx.graphml").stat().st_size > 0
    with pytest.raises(SystemExit, match="found 2 areas"):                # expect_count mismatch fails loudly
        graph_build.build({**cfg, "zones": [{**cfg["zones"][0], "expect_count": 3}]}, tmp, tmp_path / "x", tmp / "test.osm")


def test_river_bridges(tmp_path):
    """A river between grid columns 0 and 1: way 101 is a bridge in the graph; a railway bridge and a car-free
    road bridge are reported (and drawn) as river bridges outside the graph."""
    nodes = {**NODES, 70: (19.0002, 47.5005), 71: (19.0008, 47.5005), 72: (19.0002, 47.5015), 73: (19.0008, 47.5015),
             80: (19.0003, 47.4996), 81: (19.0007, 47.4996), 82: (19.0007, 47.5029), 83: (19.0003, 47.5029),
             84: (19.0005, 47.4996), 85: (19.0005, 47.5029)}
    ways = [(w, r, {**t, "name": "Régi híd", "bridge": "yes"} if w == 101 else t) for w, r, t in WAYS[:-3]] + [
        (150, [70, 71], {"railway": "rail", "bridge": "yes", "bridge:name": "Vasúti híd"}),
        (151, [72, 73], {"highway": "tertiary", "motor_vehicle": "no", "bridge": "yes", "name": "Lánc híd"}),
        (152, [70, 73], {"highway": "footway", "bridge": "yes", "name": "Gyalog híd"}),      # footways are not listed
        (160, [80, 81, 82, 83, 80], {"natural": "water", "water": "river"}),
        (161, [84, 85], {"waterway": "river", "name": "Folyó"})] + WAYS[-3:]
    write_osm(tmp_path / "river.osm", nodes, ways)
    cfg = {**CFG, "river": {"name": "Folyó"}, "checks": {"bridges": ["Régi híd"]}}
    stats = graph_build.build(cfg, tmp_path, tmp_path / "out", tmp_path / "river.osm")
    assert all(stats["checks"].values()) and stats["bridges"]["Régi híd"]["ok"]
    assert stats["river"]["crossing_bridges"] == {"Régi híd": 1}
    assert stats["river"]["bridges_not_in_graph"] == {"Lánc híd": "road", "Vasúti híd": "rail"}
    feats = json.loads((tmp_path / "out/zones.geojson").read_text(encoding="utf-8"))["features"]
    assert sorted(f["properties"]["name"] for f in feats if f["properties"].get("kind") == "bridge_not_in_graph") == [
        "Lánc híd", "Vasúti híd"]
    assert [p.name for p in render.render(tmp_path / "out", "Test")] == ["graph.png", "zones.png", "zoom.png", "bridges.png"]


OUT = ROOT / "out" / "budapest"


@pytest.mark.skipif(not (OUT / "stats.json").exists(), reason="run `citygraph build --city budapest` first")
def test_budapest_output():
    stats = json.loads((OUT / "stats.json").read_text(encoding="utf-8"))
    assert all(stats["checks"].values()), stats["checks"]
    nodes = {n["id"] for n in read(OUT / "neo4j/intersections.csv")}
    rels = read(OUT / "neo4j/streets.csv")
    assert all(r["start_id"] in nodes and r["end_id"] in nodes and float(r["length_m"]) > 0 for r in rels)
    assert not stats["warnings"]
    assert set(stats["bridges"]) == {"Megyeri híd", "Árpád híd", "Margit híd", "Széchenyi lánchíd", "Erzsébet híd",
                                     "Szabadság híd", "Petőfi híd", "Rákóczi híd", "Deák Ferenc híd"}
    assert set(stats["river"]["crossing_bridges"]) == set(stats["bridges"]) | {"K-híd", "Hajógyári híd"}  # island bridges
    assert stats["river"]["bridges_not_in_graph"] == {"Összekötő vasúti híd": "rail", "Újpesti vasúti híd": "rail"}
    assert stats["totals"]["restricted_segments"] > 0                       # the car-free Lánchíd (include_names)
    assert stats["totals"]["nodes"] >= stats["osmnx"]["nodes"]              # osmnx.graphml is built without it
