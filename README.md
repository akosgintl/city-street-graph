# city-street-graph

Turns an OpenStreetMap `.osm.pbf` into a Neo4j-ready street graph. Nodes are intersections and dead ends.
Relationships are street segments. Each city is one YAML file in `configs/`. Budapest is built and checked.
Berlin, Barcelona and other cities only need a config; the PBF is downloaded (see Get data).

## Setup (Python 3.12+, [uv](https://docs.astral.sh/uv/), run everything from the project root)
```bash
make setup        # uv sync --locked: .venv with the exact versions from uv.lock + the editable package
make test         # uv run pytest -q: synthetic OSM fixture (+ checks on out/budapest once it is built)
```
`make e2e` (= `uv run pytest -m e2e -v`, ~2 min, needs internet + a running Docker) is the real run for Budapest: it downloads
the PBF, builds through the CLI (twice, to compare the bytes), imports `out/budapest/neo4j` into a throwaway Neo4j container
(compose project `citygraph-e2e`, ports 17474/17687, removed afterwards; `E2E_KEEP=1` keeps it, password `citygraph-e2e`)
and checks counts, schema, properties, bridges, rings and the sample queries against the CSVs and `stats.json`.
Without make (same commands on Windows PowerShell):
```bash
uv sync --locked
uv run citygraph build --city budapest
uv run pytest -q
```
uv installs the Python from `.python-version` if it is missing. Dependencies: `uv add <pkg>` / `uv add --dev <pkg>` (updates
`pyproject.toml` and `uv.lock`; commit both).
In VS Code, open the folder and select the `.venv` interpreter if it isn't picked automatically. *Run and Debug* has `citygraph build/fetch --city …` entries.

## Get data
`make build CITY=berlin` downloads a missing PBF to the config's `pbf:` path (`data/raw/` is git-ignored); `make fetch CITY=…`
(= `citygraph fetch --city … [--refresh]`) only downloads. The config's `download:` sources are tried in order: BBBike city
extract first, then Geofabrik (md5-checked; `clip: true` or `[W, S, E, N]` cuts the region file, kept in `data/raw/cache/`,
with `citygraph clip`). `data/raw/<name>.source.json` (url, Last-Modified, md5, sha256, OSM timestamp) goes into `stats.json`.
"latest" files change daily: a build is reproducible for a given PBF (sha256 in `stats.json`); to pin a run, keep the PBF.
Manual fallback: put any PBF covering the bbox at the `pbf:` path (`citygraph clip in.osm.pbf out.osm.pbf --city X` cuts it).
`citygraph info --pbf file.osm.pbf` prints the bbox, the OSM replication timestamp and the sha256.

## Run
```bash
make build CITY=budapest       # = uv run citygraph build --city budapest [--pbf other.pbf] [--no-render] [--no-fetch]
make render CITY=budapest      # previews only
```
Output goes to `out/<city>/`:
`neo4j/{intersections.csv, streets.csv, import.cypher, sample_queries.cypher}`,
`graph.graphml`, `zones.geojson` (effective zone polygons + river / study-area boundary), `stats.json` (counts per zone, km, bridges,
rings, connectivity, dropped fragments, input sha256 + OSM timestamp, versions, checks) and `preview/{graph,zones,zoom}.png`.
The build is deterministic: the same PBF and config give byte-identical outputs on the same platform with the locked versions.
Only the largest connected component is kept. The other parts are listed in `stats.json` → `dropped_fragments`, with a
warning if they exceed 2 % of the nodes. If a check fails, the build exits with 1 (after writing the outputs).

## Neo4j import (5.3+)
Docker (Docker Desktop must be running) mounts `out/<city>/neo4j` as the import dir. `import.cypher` creates the schema
and loads the CSVs with LOAD CSV (empty lists → `[]`). Browser: http://localhost:7474 (neo4j / `citygraph`).
`NEO4J_HTTP_PORT` / `NEO4J_BOLT_PORT` change the host ports (defaults 7474 / 7687).
```bash
make neo4j-up CITY=budapest && make neo4j-import      # make neo4j-down before switching city (deletes the DB volume)
```
Without make (PowerShell: `$env:CITY="budapest"`):
```bash
CITY=budapest docker compose up -d                   # wait ~30 s for startup
docker compose exec neo4j cypher-shell -u neo4j -p citygraph -f /import/import.cypher
docker compose down -v
```
Paste `sample_queries.cypher` into Neo4j Browser. Its `:param` lines come from the config's `neo4j_params`, and the queries do
not hard-code any city.

## Model
```
(:Intersection {id, lat, lon, location, zone, zone_order, street_count, degree,
                kind: intersection|dead_end|bridge_end, traffic_signals, street_names[]})
  -[:STREET {id, name, names[], highway, length_m, oneway, lanes, maxspeed, ref, junction, is_bridge,
             river_crossing, bridge_name, is_tunnel, restricted, ring, zone, osm_way_ids[], geometry_wkt}]->
```
- There is one relationship per physical segment. `oneway=false`: traversable both ways, and the direction is the OSM digitisation
  direction. `oneway=true`: the direction is the travel direction (`oneway=-1` is reversed).
- The topology follows OSMnx-style simplification. Ways are split where they share a node. Pass-through nodes (two pieces
  with the same oneway status) are merged, so a segment can span several ways (`names`, `osm_way_ids`). `keep_way_endpoints: true` keeps every way end as a node.
  `id` = `<lowest OSM way id in the segment>-<piece index>`. `length_m` is geodesic (WGS84). `street_count` is counted before clipping; `degree` after.
- The network filter (`network_type`) is `drive` (the OSMnx drive filter, plus `access=no` excluded), `walk` (no oneway) or `all`.
  `include_names` keeps named ways the filter would drop, and sets `restricted=true` on them (e.g. the car-free Lánchíd).
- `river_crossing`: a bridge segment that intersects the river polygon. Such segments get the zone `river.bridge_zone`.
  Every other segment takes the outer zone of its two endpoints.

## Add a city
Copy a config, set `name`, `pbf`, `bbox` (the load window; it must contain every zone) and list `zones:` from the inside out
(`zone_order` = position; each zone minus the inner ones). Zone types:
- `osm_admin`: `admin_level` + `areas: {LABEL: OSM name or [alternatives]}`. If a name is not found, the error lists the names that exist.
- `ring`: a boundary road between the heads of two bridges (`from`, `to`) on `bank` (east/west/north/south), routed
  along the `streets` names through optional `via` anchor intersections (street-name pairs). It is closed along the
  river centerline, and the real river polygon (`natural=water`+`water=river` / `riverbank` touching `waterway=river` named `river.name`)
  is cut out. Boundary roads (15 m buffer) count as inside. `ring: <label>` tags those segments.
- `geojson`: `file` with Polygon features carrying `zone` and `zone_order`. Any `out/<city>/zones.geojson` can be replayed this way.

`checks`: `bridges` (each must be present, cross the river and connect both banks in both directions), `streets_in_zones`,
`fail_on_error`. Always checked: sanity (endpoints exist, WKT ends = node coordinates, lengths > 0, unique ids) and ring
continuity from river to river. `osm_admin` names match ignoring case and `a - b` / `a-b` spacing.

| config | zones |
|---|---|
| `budapest` | KISKORUT, NAGYKORUT, HUNGARIA (Pest rings), BUDA (west bank to the boundary-road chain), ISLAND (Margitsziget) |
| `budapest-districts` | districts V–VIII via `osm_admin` (test config) |
| `budapest-city` | whole Budapest, the 23 kerületek ordered from Deák Ferenc tér (see below) |
| `barcelona`, `berlin` | whole city: 10 districtes from Plaça de Sant Jaume / 12 Bezirke from Brandenburger Tor (Berlin untested) |

## Whole city (Superblocker-compatible)
`budapest-city`, `berlin` and `barcelona` build the network the way [Superblocker](https://github.com/tgergo1/Superblocker)
loads it live with OSMnx 2.1.1 (`graph_from_polygon(<admin polygon>, network_type="drive", simplify=True,
retain_all=False, truncate_by_edge=True)`), but offline from the PBF: `make build CITY=budapest-city` (~45 s).
- `area: {osm_admin: {name, admin_level}}`: the study area. The `bbox` must contain it + 500 m (OSMnx simplifies in that buffer).
- `clip: any`: keep nodes inside plus their neighbours, like `truncate_by_edge` (default `both`: both ends inside).
- `osmnx_compat: true`: exact OSMnx drive filter (`access=no` kept) and oneway rules; writes `osmnx.graphml`.
- zone type `osm_admin_auto` (`admin_level`, `order_from: [lon, lat]`, optional `expect_count`): admin areas of that level inside `area`, ordered
  by centroid distance. `zone` = ID-safe name (`XIII_KERULET`), `zone_name` = OSM name. Nodes outside all zones join the nearest.

`out/<city>/osmnx.graphml` is the file OSMnx `save_graphml` would write (both directions of two-way streets with `reversed`,
`osmid` lists, great-circle `length`, `geometry` on merged edges), a drop-in for OSMnx or Superblocker:
`G = ox.load_graphml("out/budapest-city/osmnx.graphml")`. On the same ways, OSMnx 2.1.1's own pipeline gives identical
nodes, edges and attributes. Only string lists are sorted (OSMnx: hash order), `created_date` is the OSM timestamp, and parallel-edge keys can be numbered differently.
`stats.json` → `osmnx` has Superblocker's counts. Budapest, BBBike 2026-10-02: 24,511 nodes, 63,994 directed edges, 37,386
physical segments, 4,740.8 km; Superblocker's published run (Overpass 2026-08-31): 24,525 / 64,026 / 37,406 / 4,743.7 km.

## Caveats
- `ring` zones, bridges and their checks live in `rings.py`. The generic path (`zones.py`) is osm_admin/geojson.
- The ring and Buda boundaries are modelling choices made in the config. The real river polygon defines the banks. Bridge-deck nodes
  over water join the nearest zone.
- `clip` keeps a relation only if one of its members lies in the bbox. A boundary polygon that fully encloses the bbox is lost, so make the
  bbox larger than the zones.
- In plain Cypher, `shortestPath` minimises hops. For metre-weighted routing, use GDS or APOC Dijkstra on `length_m`.

## Licence of the data
Map data © OpenStreetMap contributors, available under the Open Database License (ODbL 1.0, https://www.openstreetmap.org/copyright).
Every derived graph is a derivative database. Keep this attribution and share it under ODbL.
