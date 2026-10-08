"""Preview PNGs from out/<city>/: graph.png, zones.png, zoom.png and, with a river, bridges.png (the river
bridges, labelled). Dark background."""
from __future__ import annotations

import csv
import json
import math
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.collections import LineCollection  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.patches import Patch, PathPatch  # noqa: E402
from matplotlib.path import Path as MplPath  # noqa: E402
from shapely import wkt  # noqa: E402
from shapely.geometry import shape  # noqa: E402
from shapely.ops import unary_union  # noqa: E402

BG, NODE, EDGE, BRIDGE, RING, CAPTION = "#251a2b", "#ffffff", "#7a7a7a", "#6fa8dc", "#f2c14e", "#cccccc"
RIVER, OTHER_BRIDGE = "#1d3c5c", "#ff8a65"  # water fill; river bridges that are not in the graph (dashed)
ZONE_COLORS = ["#ff6b6b", "#4ecdc4", "#ffd93d", "#a78bfa", "#81c784", "#f78fb3", "#63cdda", "#e77f67",
               "#cf6a87", "#786fa6", "#f8a5c2", "#3dc1d3"]


def read_csv(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def polygons(geom) -> list:
    return [g for g in getattr(geom, "geoms", [geom]) if g.geom_type == "Polygon" and not g.is_empty]


def lines_of(geom, pxy) -> list:
    return [[pxy(x, y) for x, y in g.coords] for g in getattr(geom, "geoms", [geom]) if g.geom_type == "LineString"]


def water_patch(geom, pxy) -> PathPatch:
    """Filled river polygon; islands (interior rings) stay holes."""
    verts, codes = [], []
    for poly in polygons(geom):
        for ring in [poly.exterior, *poly.interiors]:
            pts = [pxy(x, y) for x, y in ring.coords]
            verts += pts
            codes += [MplPath.MOVETO] + [MplPath.LINETO] * (len(pts) - 2) + [MplPath.CLOSEPOLY]
    return PathPatch(MplPath(verts, codes), facecolor=RIVER, edgecolor="none", zorder=0)


def spread(ys: list[float], gap: float, lo: float, hi: float) -> list[float]:
    """Label positions for anchors sorted high -> low: at least `gap` apart, kept inside [lo, hi] where possible."""
    out = []
    for y in ys:
        out.append(min(y, hi) if not out else min(y, out[-1] - gap))
    for i in range(len(out) - 1, -1, -1):
        floor = lo if i == len(out) - 1 else out[i + 1] + gap
        out[i] = max(out[i], floor)
    return out


def label_bridges(ax, bridges: dict, crop: tuple) -> None:
    """Names in a column right of the map, in map order north -> south, each with a leader line to its bridge."""
    x0, y0, x1, y1 = crop
    note = {"in_graph": "", "restricted": "  (in the graph, restricted=true)", "road": "  (road, not in the graph)", "rail": "  (railway, not in the graph)"}
    items = []
    for (bname, kind), lns in bridges.items():
        c = unary_union([wkt.loads("LINESTRING (" + ", ".join(f"{x} {y}" for x, y in ln) + ")") for ln in lns]).centroid
        items.append((-c.y, c.x, bname, kind))
    items.sort()
    fs = 8
    gap = 1.9 * fs / 72 / ax.figure.get_figheight() * (y1 - y0) / ax.get_position().height
    tx = x1 + 0.03 * (x1 - x0)
    for (ny, x, bname, kind), ty in zip(items, spread([-i[0] for i in items], gap, y0 + gap, y1 - gap)):
        col = OTHER_BRIDGE if kind in ("road", "rail") else BRIDGE
        ax.add_line(Line2D([x, x1, tx], [-ny, ty, ty], color=col, lw=0.5, alpha=0.7, zorder=6, clip_on=False))
        ax.text(tx + 0.01 * (x1 - x0), ty, bname + note[kind], color=col, fontsize=fs, va="center", ha="left",
                clip_on=False)
    ax.legend(handles=[Line2D([], [], color=BRIDGE, lw=1.6, label="river bridge in the graph"),
                       Line2D([], [], color=OTHER_BRIDGE, lw=1.6, ls=(0, (3, 1.5)), label="river bridge not in the graph")],
              fontsize=7, framealpha=0.8, loc="upper left")


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
    bridges: dict[tuple[str, str], list] = {}  # (name, in_graph | road | rail) -> lines, projected
    for r in rels:
        cls = "bridge" if r["river_crossing"] == "true" else "ring" if r["ring"] else "edge"
        lines[cls].append([pxy(x, y) for x, y in wkt.loads(r["geometry_wkt"]).coords])
        if cls == "bridge":
            kind = "restricted" if r["restricted"] == "true" else "in_graph"
            bridges.setdefault((r["bridge_name"] or "(unnamed)", kind), []).append(lines[cls][-1])
    river = [shape(f["geometry"]) for f in fc["features"] if f["properties"].get("kind") == "river"]
    for f in fc["features"]:
        if f["properties"].get("kind") == "bridge_not_in_graph":
            bridges[f["properties"]["name"], f["properties"]["bridge"]] = lines_of(shape(f["geometry"]), pxy)
    other = [ln for (_, kind), lns in sorted(bridges.items()) if kind in ("road", "rail") for ln in lns]
    if zoom_bbox is None:  # centre quarter of the extent
        lons, lats = [float(n["lon"]) for n in nodes], [float(n["lat"]) for n in nodes]
        cx, cy, w, h = (min(lons) + max(lons)) / 2, (min(lats) + max(lats)) / 2, max(lons) - min(lons), max(lats) - min(lats)
        zoom_bbox = [cx - w / 8, cy - h / 8, cx + w / 8, cy + h / 8]
    paths = []
    crop = None
    if bridges:  # bridges.png: the stretch of river that has bridges, portrait or landscape as the river runs
        xs, ys = zip(*[p for lns in bridges.values() for ln in lns for p in ln])
        w, h = max(xs) - min(xs), max(ys) - min(ys)
        pad = 0.08 * max(w, h)
        w, h = max(w + 2 * pad, 0.7 * (h + 2 * pad)), max(h + 2 * pad, 0.5 * (w + 2 * pad))
        cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
        crop = (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)
    for mode in ("graph", "zones", "zoom") + (("bridges",) if bridges else ()):
        size = (12, 9)
        if mode == "bridges":
            size = (9, 9 * (crop[3] - crop[1]) / (crop[2] - crop[0]))
        fig, ax = plt.subplots(figsize=size, dpi=200)
        fig.patch.set_facecolor(BG)
        ax.set_facecolor(BG)
        for g in river:
            ax.add_patch(water_patch(g, pxy))
        if mode in ("zones", "zoom"):
            for f in fc["features"]:
                p = f["properties"]
                if "zone_order" not in p:
                    continue
                g = shape(f["geometry"])
                for poly in getattr(g, "geoms", [g]):
                    if poly.geom_type == "Polygon" and not poly.is_empty:
                        ax.plot(*zip(*[pxy(x, y) for x, y in poly.exterior.coords]), color=color[p["zone"]],
                                ls="--", lw=0.6, alpha=0.6, zorder=1)
        thin = big and mode in ("graph", "zones")
        lw = (1.6 if mode == "zoom" else 1.0) * (0.4 if thin else 1.0)
        for g in boundary:
            for poly in getattr(g, "geoms", [g]):
                ax.plot(*zip(*[pxy(x, y) for x, y in poly.exterior.coords]), color=CAPTION, lw=0.5, zorder=1)
        ax.add_collection(LineCollection(lines["edge"], colors=EDGE, linewidths=0.5 * lw, alpha=0.7, zorder=2))
        ax.add_collection(LineCollection(lines["ring"], colors=RING, linewidths=1.0 * lw, alpha=0.8, zorder=3))
        blw = 1.6 if mode == "bridges" else max(1.0 * lw, 1.2)  # bridges stay visible on a whole-city map
        ax.add_collection(LineCollection(lines["bridge"], colors=BRIDGE, linewidths=blw, alpha=0.95, zorder=4))
        ax.add_collection(LineCollection(other, colors=OTHER_BRIDGE, linewidths=blw, linestyles=[(0, (3, 1.5))], zorder=4))
        xs = [pxy(float(n["lon"]), float(n["lat"])) for n in nodes]
        cs = [color.get(n["zone"], NODE) if mode == "zones" else NODE for n in nodes]
        ax.scatter([p[0] for p in xs], [p[1] for p in xs],
                   s=(4 if mode == "zoom" else 1.2) * (0.25 if thin else 1),
                   c=cs, linewidths=0, zorder=5)
        ax.set_aspect("equal")
        ax.axis("off")
        if mode in ("zoom", "bridges"):
            (x0, y0), (x1, y1) = (pxy(*zoom_bbox[:2]), pxy(*zoom_bbox[2:])) if mode == "zoom" else (crop[:2], crop[2:])
            ax.set_xlim(x0, x1)
            ax.set_ylim(y0, y1)
        else:
            ax.autoscale_view()
            ax.margins(0.02)
        if mode == "zones":
            ax.legend(handles=[Patch(color=color[z], label=f"{i} {z}") for i, z in zones], fontsize=7, framealpha=0.8,
                      **({"loc": "upper left", "bbox_to_anchor": (1.0, 1.0)} if big else {"loc": "upper right"}))
        if mode == "bridges":
            label_bridges(ax, bridges, crop)
        ax.text(0.01, 0.01, f"{name} street graph ({mode}) - data (c) OpenStreetMap contributors, ODbL",
                transform=ax.transAxes, fontsize=8, color=CAPTION, family="monospace")
        (out / "preview").mkdir(exist_ok=True)
        path = out / "preview" / f"{mode}.png"
        fig.savefig(path, facecolor=BG, dpi=200, bbox_inches="tight", metadata={"Software": None})
        plt.close(fig)
        paths.append(path)
    return paths
