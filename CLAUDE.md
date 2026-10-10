# CLAUDE.md

`citygraph`: OpenStreetMap `.osm.pbf` -> Neo4j-ready street graph (intersections + street segments) with zones.
One YAML per city in `configs/`. `README.md` is the user-facing reference for the config keys, the data model and the outputs;
keep it in sync when behaviour changes.

## Tooling: Python 3.12 + uv

Everything goes through uv. Do not call `pip`, `python -m venv` or a bare `python`.

```bash
uv sync --locked                          # create/update .venv from uv.lock (= make setup)
uv run pytest -q                          # all tests (= make test), ~5 s
uv run pytest tests/test_smoke.py::test_topology -q
uv run pytest -m e2e -v                   # = make e2e: real budapest download + build + Neo4j in Docker, ~3 min
uv run citygraph build --city budapest    # = make build CITY=budapest; downloads the PBF if missing
uv run citygraph build --city budapest --no-fetch --no-render
uv run citygraph fetch|render|info|clip ...
uv add <pkg>          # runtime dependency
uv add --dev <pkg>    # dev dependency ([dependency-groups].dev)
```

- Dependencies are pinned with `==` in `pyproject.toml`; `uv.lock` is the lockfile. Commit both together.
  Outputs are byte-identical only with the locked versions, so do not upgrade packages as a side effect of other work.
- `.python-version` pins 3.12; `requires-python = ">=3.12"`.
- Run commands from the project root: configs are found as `configs/<city>.yaml` and their paths resolve against the root.
- `make neo4j-up / neo4j-import / neo4j-down` need Docker; `neo4j-down` deletes the DB volume.

## Layout

```
src/citygraph/
  cli.py           argparse entry point (build | fetch | render | info | clip), config loading
  fetch.py         PBF download from the config's `download:` sources (stdlib urllib only)
  osm_load.py      pyosmium reader: street ways, river geometry, admin areas; network filters; clip / info
  graph_build.py   the pipeline: pieces -> simplify -> zones -> clip -> checks -> stats -> exports
  zones.py         generic zone types: osm_admin, osm_admin_auto, geojson
  rings.py         river, bridges, `ring` zones and their checks (only with `river:` / `type: ring`)
  export_neo4j.py  CSV + cypher, graph.graphml, zones.geojson, stats.json
  export_osmnx.py  osmnx.graphml, identical to what OSMnx 2.1.1 would write (`osmnx_compat: true`)
  render.py        preview PNGs incl. the labelled bridges.png for river configs (matplotlib, imported lazily)
  cypher/          import.cypher and sample_queries.cypher templates (package data)
configs/           one YAML per city
tests/             synthetic OSM fixture written by test_smoke.write_osm; test_fetch uses a local HTTP server;
                   test_e2e.py is the opt-in real run of configs/budapest.yaml
data/raw/          downloaded PBFs (git-ignored)
out/<city>/        build outputs (git-ignored)
```

## Rules for changes

- The build must stay deterministic: same PBF + config -> byte-identical outputs (`test_deterministic_and_replayable`).
  Sort anything that comes out of a set or dict before writing it; no timestamps or absolute paths in outputs.
- `osmnx_compat` paths must keep matching OSMnx 2.1.1 exactly; OSMnx itself is not a dependency.
- City-specific knowledge belongs in `configs/*.yaml`, never in the code or the cypher templates.
- The default test run never touches the network or needs a real PBF (`addopts = -m "not e2e"`); `test_budapest_output`
  only runs when `out/budapest/stats.json` exists.
- `tests/test_e2e.py` (marker `e2e`) is the full real run: it downloads `data/raw/budapest.osm.pbf` if missing, overwrites `out/budapest/`,
  and starts Neo4j as the separate compose project `citygraph-e2e` on ports 17474/17687. Run it after changes to the
  pipeline, the exports or the cypher templates.
- A real build must end with `all checks ok` (exit code 1 = a check failed). `budapest` (whole city) takes ~45 s.
- Code style: `from __future__ import annotations`, type hints on signatures, short module docstrings, lines up to ~120 chars.
