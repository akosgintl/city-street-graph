"""Preview PNGs from out/<city>/: graph.png, zones.png, zoom.png (dark background)."""
from __future__ import annotations

import csv
import json
import math
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.collections import LineCollection  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402
from shapely import wkt  # noqa: E402
from shapely.geometry import shape  # noqa: E402

BG, NODE, EDGE, BRIDGE, RING, CAPTION = "#251a2b", "#ffffff", "#7a7a7a", "#6fa8dc", "#f2c14e", "#cccccc"
ZONE_COLORS = ["#ff6b6b", "#4ecdc4", "#ffd93d", "#a78bfa", "#81c784", "#f78fb3", "#63cdda", "#e77f67",
               "#cf6a87", "#786fa6", "#f8a5c2", "#3dc1d3"]


def read_csv(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def render(out: Path, name: str, zoom_bbox: list[float] | None = None) -> list[Path]:
    nodes = read_csv(out / "neo4j" / "intersections.csv")
    rels = read_csv(out / "neo4j" / "streets.csv")
    fc = json.loads((out / "zones.geojson").read_text(encoding="utf-8"))
    zones = sorted({(int(f["properties"]["zone_order"]), f["properties"]["zone"])
                    for f in fc["features"] if "zone_order" in f["properties"]})
    palette = ZONE_COLORS if len(zones) <= len(ZONE_COLORS) else [
        matplotlib.colormaps["hsv"](i * 0.618 % 1) for i in range(len(zones))]  # whole cities: many districts
    color = {z: palette[i % len(palette)] for i, z in zones}
    big = len(nodes) > 10000  # whole city: thinner lines, smaller dots
    boundary = [shape(f["geometry"]) for f in fc["features"] if f["properties"].get("kind") == "boundary"]
    k = math.cos(math.radians(sum(float(n["lat"]) for n in nodes) / len(nodes)))
    pxy = lambda lon, lat: (lon * k, lat)  # noqa: E731  (equirectangular)
    lines = {"edge": [], "bridge": [], "ring": []}
    for r in rels:
        cls = "bridge" if r["river_crossing"] == "true" else "ring" if r["ring"] else "edge"
        lines[cls].append([pxy(x, y) for x, y in wkt.loads(r["geometry_wkt"]).coords])
    if zoom_bbox is None:  # centre quarter of the extent
        lons, lats = [float(n["lon"]) for n in nodes], [float(n["lat"]) for n in nodes]
        cx, cy, w, h = (min(lons) + max(lons)) / 2, (min(lats) + max(lats)) / 2, max(lons) - min(lons), max(lats) - min(lats)
        zoom_bbox = [cx - w / 8, cy - h / 8, cx + w / 8, cy + h / 8]
    paths = []
    for mode in ("graph", "zones", "zoom"):
        fig, ax = plt.subplots(figsize=(12, 9), dpi=200)
        fig.patch.set_facecolor(BG)
        ax.set_facecolor(BG)
        if mode != "graph":
            for f in fc["features"]:
                p = f["properties"]
                if "zone_order" not in p:
                    continue
                g = shape(f["geometry"])
                for poly in getattr(g, "geoms", [g]):
                    if poly.geom_type == "Polygon" and not poly.is_empty:
                        ax.plot(*zip(*[pxy(x, y) for x, y in poly.exterior.coords]), color=color[p["zone"]],
                                ls="--", lw=0.6, alpha=0.6, zorder=1)
        thin = big and mode != "zoom"
        lw = (1.6 if mode == "zoom" else 1.0) * (0.4 if thin else 1.0)
        for g in boundary:
            for poly in getattr(g, "geoms", [g]):
                ax.plot(*zip(*[pxy(x, y) for x, y in poly.exterior.coords]), color=CAPTION, lw=0.5, zorder=1)
        ax.add_collection(LineCollection(lines["edge"], colors=EDGE, linewidths=0.5 * lw, alpha=0.7, zorder=2))
        ax.add_collection(LineCollection(lines["ring"], colors=RING, linewidths=1.0 * lw, alpha=0.8, zorder=3))
        ax.add_collection(LineCollection(lines["bridge"], colors=BRIDGE, linewidths=1.0 * lw, alpha=0.9, zorder=4))
        xs = [pxy(float(n["lon"]), float(n["lat"])) for n in nodes]
        cs = [color.get(n["zone"], NODE) if mode == "zones" else NODE for n in nodes]
        ax.scatter([p[0] for p in xs], [p[1] for p in xs], s=(4 if mode == "zoom" else 1.2) * (0.25 if thin else 1),
                   c=cs, linewidths=0, zorder=5)
        ax.set_aspect("equal")
        ax.axis("off")
        if mode == "zoom":
            (x0, y0), (x1, y1) = pxy(*zoom_bbox[:2]), pxy(*zoom_bbox[2:])
            ax.set_xlim(x0, x1)
            ax.set_ylim(y0, y1)
        else:
            ax.autoscale_view()
            ax.margins(0.02)
        if mode == "zones":
            ax.legend(handles=[Patch(color=color[z], label=f"{i} {z}") for i, z in zones], fontsize=7, framealpha=0.8,
                      **({"loc": "upper left", "bbox_to_anchor": (1.0, 1.0)} if big else {"loc": "upper right"}))
        ax.text(0.01, 0.01, f"{name} street graph ({mode}) - data (c) OpenStreetMap contributors, ODbL",
                transform=ax.transAxes, fontsize=8, color=CAPTION, family="monospace")
        (out / "preview").mkdir(exist_ok=True)
        path = out / "preview" / f"{mode}.png"
        fig.savefig(path, facecolor=BG, dpi=200, bbox_inches="tight", metadata={"Software": None})
        plt.close(fig)
        paths.append(path)
    return paths
