// Sample queries (plain Cypher, Neo4j 5.3+). City-specific values come only from the :param lines above
// (newer cypher-shell versions also accept `:param {name: value}`).

// 1. Shortest path (fewest segments) between an intersection of $from_streets and one on $to_street.
//    shortestPath() minimises hops, not metres, and the undirected pattern ignores oneway.
//    Metre-weighted: GDS gds.shortestPath.dijkstra (relationshipWeightProperty: 'length_m') or APOC.
MATCH (a:Intersection) WHERE all(s IN $from_streets WHERE s IN a.street_names)
WITH a ORDER BY a.id LIMIT 1
MATCH (b:Intersection) WHERE $to_street IN b.street_names
WITH a, b ORDER BY b.id LIMIT 1
MATCH p = shortestPath((a)-[:STREET*..300]-(b))
RETURN length(p) AS segments,
       round(reduce(m = 0.0, r IN relationships(p) | m + r.length_m)) AS metres,
       [r IN relationships(p) | r.name] AS streets;

// 2. Signalised intersections on ring $ring.
MATCH (n:Intersection)-[:STREET {ring: $ring}]-()
WHERE n.traffic_signals
RETURN DISTINCT n.id, n.street_names, n.lat, n.lon
ORDER BY n.lat DESC;

// 3. River bridges with segment counts and the zones they connect.
MATCH (a:Intersection)-[r:STREET]->(b:Intersection)
WHERE r.river_crossing
WITH r.bridge_name AS bridge, count(r) AS segments, round(sum(r.length_m)) AS metres,
     collect(a.zone) + collect(b.zone) AS zones
UNWIND zones AS z
RETURN bridge, segments, metres, collect(DISTINCT z) AS endpoint_zones
ORDER BY bridge;

// 4. Ring segments by street name.
MATCH ()-[r:STREET]->() WHERE r.ring IS NOT NULL
RETURN r.ring AS ring, r.name AS street, count(*) AS segments, round(sum(r.length_m)) / 1000.0 AS km
ORDER BY ring, km DESC;

// 5. Degree distribution.
MATCH (n:Intersection)
RETURN n.degree AS degree, count(*) AS intersections
ORDER BY degree;

// 6. Intersections and kinds per zone, inside-out.
MATCH (n:Intersection)
RETURN n.zone_order AS zone_order, n.zone AS zone, count(*) AS intersections,
       sum(CASE n.kind WHEN 'dead_end' THEN 1 ELSE 0 END) AS dead_ends,
       sum(CASE WHEN n.traffic_signals THEN 1 ELSE 0 END) AS signalised
ORDER BY zone_order;

// 7. Segments crossing zones (radials leaving a ring, bridges).
MATCH (a:Intersection)-[r:STREET]->(b:Intersection)
WHERE a.zone <> b.zone
RETURN a.zone AS from_zone, b.zone AS to_zone, count(*) AS segments
ORDER BY segments DESC;

// 8. Nearest intersections to $here (uses the point index).
WITH point($here) AS here
MATCH (n:Intersection)
WHERE point.distance(n.location, here) < 150
RETURN n.id, n.street_names, round(point.distance(n.location, here)) AS metres
ORDER BY metres LIMIT 5;
