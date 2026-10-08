"""citygraph build | fetch | render | info | clip   (run from the project root)"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import yaml

from . import fetch, graph_build, osm_load


def load_config(args) -> tuple[dict, Path, Path]:
    """-> (config, project root, out dir). Paths in a config are relative to the project root
    (the parent of the configs/ directory)."""
    path = Path(args.config or f"configs/{args.city}.yaml").resolve()
    if not path.is_file():
        have = sorted(p.stem for p in path.parent.glob("*.yaml")) if path.parent.is_dir() else []
        raise SystemExit(f"config not found: {path}\navailable: {have or 'none (run from the project root)'}")
    cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    for key in ("bbox", "zones"):
        if key not in cfg:
            raise SystemExit(f"{path.name}: missing required key `{key}`")
    if "river" in cfg and not (cfg["river"] or {}).get("name"):
        raise SystemExit(f"{path.name}: `river:` needs a `name:` (the waterway=river name, e.g. Duna)")
    root = path.parent.parent
    return cfg, root, root / "out" / path.stem


def existing_pbf(path: Path) -> Path:
    if not path.is_file():
        raise SystemExit(f"PBF not found: {path} - see README > Get data")
    return path


def do_render(cfg, out: Path) -> None:
    try:
        from . import render  # lazy: matplotlib is only needed here
        for p in render.render(out, cfg.get("name", out.name), (cfg.get("render") or {}).get("zoom_bbox")):
            print(p)
    except Exception as e:  # previews are optional; the graph is already written
        print(f"warning: preview rendering failed: {e!r}")


def cmd_build(args) -> int:
    cfg, root, out = load_config(args)
    if args.pbf:
        pbf = existing_pbf(Path(args.pbf).resolve())
    else:
        pbf = root / cfg.get("pbf", "")
        if not args.no_fetch:
            fetch.fetch(cfg, root, pbf)
        existing_pbf(pbf)
    t = time.time()
    stats = graph_build.build(cfg, root, out, pbf)
    if not args.no_render:
        do_render(cfg, out)
    print(json.dumps({k: stats[k] for k in ("zones", "totals", "connectivity", "checks")}, ensure_ascii=False, indent=1))
    for w in stats["warnings"]:
        print(f"warning: {w}")
    failed = [k for k, ok in stats["checks"].items() if not ok]
    print(f"wrote {out} in {time.time() - t:.0f}s" + (f"; FAILED checks: {failed}" if failed else "; all checks ok"))
    return 1 if failed and (cfg.get("checks") or {}).get("fail_on_error", True) else 0


def cmd_fetch(args) -> int:
    cfg, root, _ = load_config(args)
    pbf = root / cfg.get("pbf", "")
    rec = fetch.fetch(cfg, root, pbf, refresh=args.refresh)
    print(json.dumps(rec or {"pbf": str(pbf), "note": "already present (no source.json); --refresh re-downloads"}, indent=1))
    return 0


def cmd_render(args) -> int:
    cfg, _, out = load_config(args)
    do_render(cfg, out)
    return 0


def cmd_info(args) -> int:
    pbf = existing_pbf(Path(args.pbf))
    print(json.dumps({**osm_load.header(pbf), "sha256": osm_load.sha256(pbf)}, indent=1))
    return 0


def cmd_clip(args) -> int:
    bbox = args.bbox
    if bbox is None:
        cfg, _, _ = load_config(args)
        bbox = cfg["bbox"]
    osm_load.clip(existing_pbf(Path(args.src)), Path(args.dst), bbox)
    print(json.dumps(osm_load.header(Path(args.dst)), indent=1))
    return 0


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    p = argparse.ArgumentParser(prog="citygraph", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, fn in (("build", cmd_build), ("fetch", cmd_fetch), ("render", cmd_render)):
        s = sub.add_parser(name)
        s.add_argument("--city", default="budapest", help="configs/<city>.yaml")
        s.add_argument("--config", help="explicit config path (overrides --city)")
        s.set_defaults(fn=fn)
        if name == "build":
            s.add_argument("--pbf", help="override the PBF path from the config")
            s.add_argument("--no-render", action="store_true", help="skip preview PNGs")
            s.add_argument("--no-fetch", action="store_true", help="do not download a missing PBF")
        if name == "fetch":
            s.add_argument("--refresh", action="store_true", help="download again even if the PBF exists")
    s = sub.add_parser("info", help="print PBF bbox / timestamp / sha256")
    s.add_argument("--pbf", required=True)
    s.set_defaults(fn=cmd_info)
    s = sub.add_parser("clip", help="cut a large PBF down to a bbox (or a city config's bbox)")
    s.add_argument("src")
    s.add_argument("dst")
    s.add_argument("--bbox", type=float, nargs=4, metavar=("W", "S", "E", "N"))
    s.add_argument("--city", help="take the bbox from configs/<city>.yaml")
    s.add_argument("--config")
    s.set_defaults(fn=cmd_clip)
    args = p.parse_args(argv)
    if args.cmd == "clip" and not (args.bbox or args.city or args.config):
        p.error("clip needs --bbox or --city")
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
