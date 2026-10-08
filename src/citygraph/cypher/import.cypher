// Street graph -> Neo4j 5.3+ via LOAD CSV.
// Put intersections.csv and streets.csv in the server's import directory, then:
//   cypher-shell -u neo4j -p <password> -f import.cypher
// (In Neo4j Browser, prefix each LOAD CSV statement with :auto.)
// Empty fields become null / [] (CASE instead of nullIf(), which needs a newer 5.x); lists are ';'-separated.

// ---- schema
CREATE CONSTRAINT intersection_id IF NOT EXISTS FOR (n:Intersection) REQUIRE n.id IS UNIQUE;
CREATE INDEX intersection_zone IF NOT EXISTS FOR (n:Intersection) ON (n.zone);
CREATE POINT INDEX intersection_location IF NOT EXISTS FOR (n:Intersection) ON (n.location);
CREATE INDEX street_id IF NOT EXISTS FOR ()-[r:STREET]-() ON (r.id);
CREATE INDEX street_name IF NOT EXISTS FOR ()-[r:STREET]-() ON (r.name);
CREATE INDEX street_ring IF NOT EXISTS FOR ()-[r:STREET]-() ON (r.ring);

// ---- nodes
LOAD CSV WITH HEADERS FROM 'file:///intersections.csv' AS row
CALL {
  WITH row
  MERGE (n:Intersection {id: row.id})
  SET n.lat             = toFloat(row.lat),
      n.lon             = toFloat(row.lon),
      n.location        = point({latitude: toFloat(row.lat), longitude: toFloat(row.lon)}),
      n.zone            = row.zone,
      n.zone_order      = toInteger(row.zone_order),
      n.street_count    = toInteger(row.street_count),
      n.degree          = toInteger(row.degree),
      n.kind            = row.kind,
      n.traffic_signals = toBoolean(row.traffic_signals),
      n.street_names    = CASE WHEN coalesce(row.street_names, '') = '' THEN [] ELSE split(row.street_names, ';') END
} IN TRANSACTIONS OF 1000 ROWS;

// ---- relationships (one per physical segment; oneway=false means traversable both ways)
LOAD CSV WITH HEADERS FROM 'file:///streets.csv' AS row
CALL {
  WITH row
  MATCH (a:Intersection {id: row.start_id})
  MATCH (b:Intersection {id: row.end_id})
  MERGE (a)-[r:STREET {id: row.id}]->(b)
  SET r.name           = CASE WHEN row.name = '' THEN null ELSE row.name END,
      r.names          = CASE WHEN coalesce(row.names, '') = '' THEN [] ELSE split(row.names, ';') END,
      r.highway        = row.highway,
      r.length_m       = toFloat(row.length_m),
      r.oneway         = toBoolean(row.oneway),
      r.lanes          = CASE WHEN row.lanes = '' THEN null ELSE row.lanes END,
      r.maxspeed       = CASE WHEN row.maxspeed = '' THEN null ELSE row.maxspeed END,
      r.ref            = CASE WHEN row.ref = '' THEN null ELSE row.ref END,
      r.junction       = CASE WHEN row.junction = '' THEN null ELSE row.junction END,
      r.is_bridge      = toBoolean(row.is_bridge),
      r.river_crossing = toBoolean(row.river_crossing),
      r.bridge_name    = CASE WHEN row.bridge_name = '' THEN null ELSE row.bridge_name END,
      r.is_tunnel      = toBoolean(row.is_tunnel),
      r.restricted     = toBoolean(row.restricted),
      r.ring           = CASE WHEN row.ring = '' THEN null ELSE row.ring END,
      r.zone           = row.zone,
      r.osm_way_ids    = CASE WHEN coalesce(row.osm_way_ids, '') = '' THEN [] ELSE split(row.osm_way_ids, ';') END,
      r.geometry_wkt   = row.geometry_wkt
} IN TRANSACTIONS OF 1000 ROWS;
