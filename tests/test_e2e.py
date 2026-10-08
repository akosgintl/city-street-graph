"""End-to-end run for central Budapest (configs/budapest-rings.yaml) with nothing mocked: download the PBF, build through the CLI, import into Neo4j
in Docker and query it. Opt-in (`make e2e` = `pytest -m e2e`): needs internet and a running Docker daemon.

It writes the real data/raw/budapest.osm.pbf and out/budapest-rings/. Neo4j runs as its own compose project
(citygraph-e2e, ports 17474/17687), so a `make neo4j-up` database is left alone; E2E_KEEP=1 keeps it running."""
import base64
import csv
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import networkx as nx
import pytest
import yaml

from citygraph import fetch

pytestmark = pytest.mark.e2e

ROOT = Path(__file__).resolve().parents[1]
CITY = "budapest-rings"
OUT = ROOT / "out" / CITY
CFG = yaml.safe_load((ROOT / "configs" / f"{CITY}.yaml").read_text(encoding="utf-8"))
PBF = ROOT / CFG["pbf"]
PASSWORD = "citygraph-e2e"
HTTP_PORT, BOLT_PORT = 17474, 17687
COMPOSE = ["docker", "compose", "-p", "citygraph-e2e"]
ENV = {**os.environ, "CITY": CITY, "NEO4J_PASSWORD": PASSWORD,
       "NEO4J_HTTP_PORT": str(HTTP_PORT), "NEO4J_BOLT_PORT": str(BOLT_PORT)}
DETERMINISTIC = ("neo4j/streets.csv", "neo4j/intersections.csv", "neo4j/import.cypher", "neo4j/sample_queries.cypher",
                 "zones.geojson", "stats.json", "graph.graphml")
PREVIEWS = ("bridges.png", "graph.png", "zones.png", "zoom.png")           # bridges.png: configs with a `river:`


def run(cmd, **kw) -> subprocess.CompletedProcess:
    p = subprocess.run(cmd, cwd=ROOT, env=ENV, capture_output=True, text=True, encoding="utf-8", **kw)
    assert p.returncode == 0, f"{' '.join(map(str, cmd))} -> exit {p.returncode}\n{p.stdout[-3000:]}\n{p.stderr[-3000:]}"
    return p


def citygraph(*args) -> subprocess.CompletedProcess:
    return run([sys.executable, "-m", "citygraph", *args])


def read(path: Path) -> list[dict]:
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def cypher(statement: str, **params) -> list[list]:
    """Rows of one statement through the Neo4j HTTP API."""
    req = urllib.request.Request(
        f"http://localhost:{HTTP_PORT}/db/neo4j/tx/commit",
        json.dumps({"statements": [{"statement": statement, "parameters": params}]}).encode(),
        {"Content-Type": "application/json",
         "Authorization": "Basic " + base64.b64encode(f"neo4j:{PASSWORD}".encode()).decode()})
    with urllib.request.urlopen(req, timeout=120) as r:
        body = json.load(r)
    assert not body["errors"], body["errors"]
    return [d["row"] for d in body["results"][0]["data"]]


def cypher_shell(*args) -> subprocess.CompletedProcess:
    return run([*COMPOSE, "exec", "-T", "neo4j", "cypher-shell", "-u", "neo4j", "-p", PASSWORD, "--format", "plain", *args])


@pytest.fixture(scope="module")
def built() -> dict:
    """citygraph fetch + build, exactly as a user runs them."""
    citygraph("fetch", "--city", CITY)
    p = citygraph("build", "--city", CITY)
    assert "all checks ok" in p.stdout, p.stdout[-2000:]
    stats = json.loads((OUT / "stats.json").read_text(encoding="utf-8"))
    return {"stats": stats, "nodes": read(OUT / "neo4j/intersections.csv"), "rels": read(OUT / "neo4j/streets.csv")}


@pytest.fixture(scope="module")
def neo4j(built):
    """Fresh Neo4j container with out/budapest-rings/neo4j mounted, import.cypher loaded."""
    if not shutil.which("docker") or subprocess.run(["docker", "info"], capture_output=True).returncode:
        pytest.fail("Docker is not running (the e2e test needs it)")
    run([*COMPOSE, "down", "-v", "--remove-orphans"])
    run([*COMPOSE, "up", "-d"], timeout=600)
    try:
        for _ in range(60):
            try:
                cypher("RETURN 1")
                break
            except (urllib.error.URLError, OSError):
                time.sleep(3)
        else:
            pytest.fail("Neo4j not reachable after 3 min:\n" + run([*COMPOSE, "logs", "--tail", "50", "neo4j"]).stdout)
        cypher_shell("-f", "/import/import.cypher")
        yield
    finally:
        if not os.environ.get("E2E_KEEP"):
            subprocess.run([*COMPOSE, "down", "-v"], cwd=ROOT, env=ENV, capture_output=True)


# ---------------------------------------------------------------- fetch + build

def test_fetch_provenance(built):
    stats, src = built["stats"], fetch.source_path(PBF)
    assert PBF.stat().st_size > 10_000_000
    assert stats["input"]["bytes"] == PBF.stat().st_size and stats["input"]["osm_timestamp"]
    info = json.loads(citygraph("info", "--pbf", str(PBF)).stdout)
    assert info["sha256"] == stats["input"]["sha256"]
    if src.exists():                                           # written by the download; absent for a hand-placed PBF
        rec = json.loads(src.read_text(encoding="utf-8"))
        assert rec["sha256"] == stats["input"]["sha256"] and rec["url"].startswith("https://")


def test_build_outputs(built):
    stats, nodes, rels = built["stats"], built["nodes"], built["rels"]
    assert all(stats["checks"].values()) and all(stats["sanity"].values()) and not stats["warnings"]
    for f in (*DETERMINISTIC, *(f"preview/{p}" for p in PREVIEWS)):
        assert (OUT / f).stat().st_size > 0, f
    assert all((OUT / "preview" / f).read_bytes()[:8] == b"\x89PNG\r\n\x1a\n" for f in PREVIEWS)
    assert (len(nodes), len(rels)) == (stats["totals"]["nodes"], stats["totals"]["segments"])
    assert 3000 < len(nodes) < 5000 and 500 < stats["totals"]["km"] < 700           # central Budapest, drive network
    assert abs(sum(float(r["length_m"]) for r in rels) / 1000 - stats["totals"]["km"]) < 0.1
    ids = {n["id"] for n in nodes}
    assert all(r["start_id"] in ids and r["end_id"] in ids for r in rels)
    zones = [z.get("zone") or next(iter(z["areas"])) for z in CFG["zones"]]
    assert list(stats["zones"]) == [*zones, CFG["river"]["bridge_zone"]]            # inside -> outside, bridges last
    assert all(stats["zones"][z]["nodes"] > 0 for z in zones)
    assert {n["zone"] for n in nodes} == set(zones)
    assert stats["connectivity"]["largest_scc_share"] > 0.98


def test_bridges_and_rings(built):
    stats, rels = built["stats"], built["rels"]
    assert set(stats["bridges"]) == set(CFG["checks"]["bridges"])
    for name, b in stats["bridges"].items():
        assert b["ok"] and b["river_crossing"] and b["left_to_right"] and b["right_to_left"], name
    crossing = [r for r in rels if r["river_crossing"] == "true"]
    assert {r["bridge_name"] for r in crossing} == set(CFG["checks"]["bridges"])
    assert {r["zone"] for r in crossing} == {CFG["river"]["bridge_zone"]}
    assert set(stats["rings"]) == {z["ring"] for z in CFG["zones"] if z.get("ring")}
    assert all(r["continuous_undirected"] for r in stats["rings"].values())
    assert any(r["restricted"] == "true" and "Széchenyi lánchíd" in r["names"] for r in rels)   # include_names


def test_graphml_and_zones_geojson(built):
    stats = built["stats"]
    G = nx.read_graphml(OUT / "graph.graphml")
    assert (G.number_of_nodes(), G.number_of_edges()) == (stats["totals"]["nodes"], stats["totals"]["segments"])
    assert nx.is_weakly_connected(G)
    feats = json.loads((OUT / "zones.geojson").read_text(encoding="utf-8"))["features"]
    assert {f["properties"].get("zone") for f in feats} >= {n["zone"] for n in built["nodes"]}


def test_rebuild_is_byte_identical(built, tmp_path):
    (tmp_path / "configs").mkdir()
    shutil.copy(ROOT / "configs" / f"{CITY}.yaml", tmp_path / "configs")
    p = citygraph("build", "--config", str(tmp_path / "configs" / f"{CITY}.yaml"), "--pbf", str(PBF), "--no-render")
    assert "all checks ok" in p.stdout
    for f in DETERMINISTIC:
        assert (OUT / f).read_bytes() == (tmp_path / "out" / CITY / f).read_bytes(), f


def test_render_command(built):
    for f in OUT.glob("preview/*.png"):
        f.unlink()
    citygraph("render", "--city", CITY)
    assert sorted(f.name for f in OUT.glob("preview/*.png")) == list(PREVIEWS)


# ---------------------------------------------------------------- Neo4j

def test_neo4j_counts_and_schema(built, neo4j):
    stats = built["stats"]
    assert cypher("MATCH (n:Intersection) RETURN count(n)") == [[stats["totals"]["nodes"]]]
    assert cypher("MATCH ()-[r:STREET]->() RETURN count(r)") == [[stats["totals"]["segments"]]]
    assert cypher("MATCH (n) WHERE NOT n:Intersection RETURN count(n)") == [[0]]
    names = {r[0] for r in cypher("SHOW INDEXES YIELD name, state WHERE state = 'ONLINE' RETURN name")}
    assert names >= {"intersection_id", "intersection_zone", "intersection_location", "street_id", "street_name", "street_ring"}
    assert cypher("SHOW CONSTRAINTS YIELD name RETURN name") == [["intersection_id"]]
    km = cypher("MATCH ()-[r:STREET]->() RETURN sum(r.length_m) / 1000.0")[0][0]
    assert abs(km - stats["totals"]["km"]) < 0.1
    per_zone = dict(cypher("MATCH (n:Intersection) RETURN n.zone, count(*)"))
    assert per_zone == {z: v["nodes"] for z, v in stats["zones"].items() if v["nodes"]}
    kinds = dict(cypher("MATCH (n:Intersection) RETURN n.kind, count(*)"))
    assert kinds == stats["totals"]["kinds"]
    assert cypher("MATCH ()-[r:STREET]->() WHERE r.oneway RETURN count(r)") == [[stats["totals"]["oneway_segments"]]]
    assert cypher("MATCH ()-[r:STREET]->() WHERE r.name IS NULL RETURN count(r)") == [[stats["totals"]["unnamed_segments"]]]


def test_neo4j_properties_match_csv(built, neo4j):
    r = max(built["rels"], key=lambda r: len(r["osm_way_ids"]))                    # a segment merged from many ways
    row = cypher("MATCH (a)-[r:STREET {id: $id}]->(b) RETURN a.id, b.id, r.names, r.osm_way_ids, r.length_m, r.oneway, "
                 "r.geometry_wkt, r.zone", id=r["id"])[0]
    assert row == [r["start_id"], r["end_id"], r["names"].split(";") if r["names"] else [], r["osm_way_ids"].split(";"),
                   float(r["length_m"]), r["oneway"] == "true", r["geometry_wkt"], r["zone"]]
    n = max(built["nodes"], key=lambda n: len(n["street_names"]))
    row = cypher("MATCH (n:Intersection {id: $id}) RETURN n.lat, n.lon, n.location.latitude, n.location.srid, n.zone, "
                 "n.zone_order, n.degree, n.street_count, n.traffic_signals, n.street_names, COUNT { (n)-[:STREET]-() }",
                 id=n["id"])[0]
    assert row[:10] == [float(n["lat"]), float(n["lon"]), float(n["lat"]), 4326, n["zone"], int(n["zone_order"]),
                        int(n["degree"]), int(n["street_count"]), n["traffic_signals"] == "true", n["street_names"].split(";")]
    assert cypher("MATCH (n:Intersection) WHERE n.degree <> COUNT { (n)-[:STREET]-() } + COUNT { (n)-[:STREET]->(n) } "
                  "RETURN count(n)") == [[0]]                                      # a self-loop counts twice in `degree`
    assert cypher("MATCH (n:Intersection) WHERE NOT (n)-[:STREET]-() RETURN count(n)") == [[0]]


def test_neo4j_bridges_and_rings(built, neo4j):
    stats = built["stats"]
    got = dict(cypher("MATCH ()-[r:STREET]->() WHERE r.river_crossing RETURN r.bridge_name, count(*)"))
    assert got == stats["river"]["crossing_bridges"]
    rings = {name: [seg, km] for name, seg, km in cypher(
        "MATCH ()-[r:STREET]->() WHERE r.ring IS NOT NULL RETURN r.ring, count(*), sum(r.length_m) / 1000.0")}
    assert set(rings) == set(stats["rings"])
    for name, s in stats["rings"].items():
        assert rings[name][0] == s["segments"] and abs(rings[name][1] - s["km"]) < 0.01, name
        # the ring is one chain in the database too: from its first node every ring node is reachable
        assert cypher("MATCH (s:Intersection {id: $start}), (e:Intersection {id: $end}) "
                      "MATCH p = shortestPath((s)-[:STREET*..400]-(e)) WHERE all(r IN relationships(p) WHERE r.ring = $ring) "
                      "RETURN count(p)", start=str(s["start_node"]), end=str(s["end_node"]), ring=name) == [[1]], name


def test_neo4j_sample_queries(built, neo4j):
    p = cypher_shell("-f", "/import/sample_queries.cypher")                        # the whole file, :param lines included
    for col in ("segments, metres, streets", "bridge, segments, metres, endpoint_zones", "degree, intersections",
                "zone_order, zone, intersections", "from_zone, to_zone, segments", "n.id, n.street_names, metres"):
        assert col in p.stdout, col
    q = CFG["neo4j_params"]
    path = cypher("MATCH (a:Intersection) WHERE all(s IN $from_streets WHERE s IN a.street_names) WITH a ORDER BY a.id LIMIT 1 "
                  "MATCH (b:Intersection) WHERE $to_street IN b.street_names WITH a, b ORDER BY b.id LIMIT 1 "
                  "MATCH p = shortestPath((a)-[:STREET*..300]-(b)) "
                  "RETURN length(p), reduce(m = 0.0, r IN relationships(p) | m + r.length_m), [r IN relationships(p) | r.name]",
                  **q)
    hops, metres, streets = path[0]
    assert hops > 5 and 2000 < metres < 6000                                       # Oktogon -> Clark Ádám tér, ~3 km
    assert any(s and "híd" in s for s in streets)                                  # has to cross the Danube
    near = cypher("WITH point($here) AS here MATCH (n:Intersection) WHERE point.distance(n.location, here) < 150 "
                  "RETURN n.street_names, point.distance(n.location, here) AS m ORDER BY m LIMIT 5", **q)
    assert near and any("Rákóczi út" in names for names, _ in near)                # Astoria
    signals = cypher("MATCH (n:Intersection)-[:STREET {ring: $ring}]-() WHERE n.traffic_signals RETURN count(DISTINCT n)", **q)
    assert signals[0][0] > 10


def test_neo4j_import_is_idempotent(built, neo4j):
    cypher_shell("-f", "/import/import.cypher")
    stats = built["stats"]
    assert cypher("MATCH (n) RETURN count(n)") == [[stats["totals"]["nodes"]]]
    assert cypher("MATCH ()-[r]->() RETURN count(r)") == [[stats["totals"]["segments"]]]
