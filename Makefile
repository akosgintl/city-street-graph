CITY ?= budapest
NEO4J_PASSWORD ?= citygraph
CG := uv run citygraph
export CITY NEO4J_PASSWORD

.PHONY: setup fetch build render test e2e neo4j-up neo4j-import neo4j-down clean

setup:            ## .venv with the exact versions from uv.lock + editable package
	uv sync --locked

fetch:            ## download data/raw PBF for CITY (build does this when it is missing)
	$(CG) fetch --city $(CITY)

build:            ## make build CITY=budapest
	$(CG) build --city $(CITY)

render:
	$(CG) render --city $(CITY)

test:
	uv run pytest -q

e2e:              ## real run for Budapest: download, build, Neo4j import in Docker, queries (E2E_KEEP=1 keeps the container)
	uv run pytest -m e2e -v

neo4j-up:         ## Neo4j 5 with out/$(CITY)/neo4j mounted as the import dir
	docker compose up -d

neo4j-import:     ## LOAD CSV import into the running container
	@for i in $$(seq 40); do docker compose exec -T neo4j cypher-shell -u neo4j -p $(NEO4J_PASSWORD) "RETURN 1" >/dev/null 2>&1 && exit 0; sleep 3; done; \
	  echo "Neo4j not reachable after 2 min (is Docker running? try: docker compose logs neo4j)"; exit 1
	docker compose exec -T neo4j cypher-shell -u neo4j -p $(NEO4J_PASSWORD) -f /import/import.cypher

neo4j-down:       ## stop and delete the database volume (do this before importing another city)
	docker compose down -v

clean:
	rm -rf out/$(CITY)
