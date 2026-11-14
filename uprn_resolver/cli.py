"""Command line interface.

Pipeline order: download -> stage -> build -> extras (provenance, poi, osm, ppd, address, outlines) -> resolve ->
national -> validate -> publish. Every module is imported lazily so a missing optional dependency only
affects the command that needs it. Optional sources warn and continue; per-LAD loops are resumable
(``--rebuild`` / ``--restage`` force a redo, otherwise finished LADs are skipped).
"""

from __future__ import annotations

import argparse
import importlib
import json
import shutil
import sys
from pathlib import Path

DATASETS = ["open_uprn", "lids", "open_toid", "openmap_local", "onsud"]
# Optional download sources run by default (as before, plus ros) and opt-in extras (name -> help)
DEFAULT_SOURCES = ["core", "hmlr", "streets", "epc", "inspire", "ros"]
EXTRA_SOURCES = ["provenance", "ostn15", "osm", "osm_buildings", "msbuildings", "overture"]
SOURCES = DEFAULT_SOURCES + EXTRA_SOURCES


def _lad_args(
    sp: argparse.ArgumentParser, rebuild_help: str = "with --all: redo LADs already done", required: bool = True
) -> None:
    g = sp.add_mutually_exclusive_group(required=required)
    g.add_argument("--lad", help="LAD code, e.g. E09000019")
    g.add_argument("--all", action="store_true", help="every LAD with the required inputs (resumable)")
    sp.add_argument("--rebuild", action="store_true", help=rebuild_help)


def _release_extra_args(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--no-stac", action="store_true", help="skip the stac/ catalogue")
    sp.add_argument(
        "--national-files", action="store_true", help="also write national/<table>/part-*.parquet (scratch disk)"
    )
    sp.add_argument(
        "--changelog-against", type=Path, default=None, metavar="DIR", help="previous release dir: write CHANGELOG.*"
    )
    sp.add_argument(
        "--source-manifest", type=Path, default=None, metavar="FILE", help="data/parquet/manifest.json for the manifest"
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="uprn-resolver",
        description="Open (OGL-only) UPRN -> point, TOID, approximate building outline.",
    )
    p.add_argument(
        "--data-dir",
        type=Path,
        default=Path("data"),
        help="base directory containing raw/, parquet/, staged/, out/ (default: ./data)",
    )
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("download", help="download source datasets (converted to national Parquet)")
    d.add_argument(
        "--datasets", nargs="+", choices=DATASETS, default=None, help="subset of OS/ONS datasets (default: all)"
    )
    d.add_argument(
        "--areas", nargs="+", default=None, metavar="TQ", help="OS grid squares to fetch for OpenMap Local, e.g. TQ"
    )
    d.add_argument(
        "--sources",
        nargs="+",
        choices=SOURCES,
        default=None,
        metavar="SRC",
        help=f"what to fetch; default: {' '.join(DEFAULT_SOURCES)} (core = --datasets). "
        f"Opt-in extras: {' '.join(EXTRA_SOURCES)}",
    )
    d.add_argument(
        "--pbf",
        type=Path,
        default=None,
        help="OSM PBF for osm / osm_buildings (default: newest *.osm.pbf in <data-dir> or raw/osm, "
        "else download the Geofabrik GB extract)",
    )

    s = sub.add_parser("stage", help="normalise and filter to one LAD, or all GB LADs")
    sg = s.add_mutually_exclusive_group(required=True)
    sg.add_argument("--lad", help="LAD code, e.g. E09000019")
    sg.add_argument(
        "--all", action="store_true", help="stage every GB LAD in national ONSUD (skips LADs already staged)"
    )
    s.add_argument("--restage", action="store_true", help="with --all: restage LADs already staged")
    s.add_argument("--no-epc", action="store_true", help="with --all: skip EPC staging")

    b = sub.add_parser("build", help="build outputs for one LAD, or all staged LADs")
    g = b.add_mutually_exclusive_group(required=True)
    g.add_argument("--lad", help="LAD code, e.g. E09000019")
    g.add_argument(
        "--all", action="store_true", help="build every LAD under <data-dir>/staged (skips LADs already built)"
    )
    b.add_argument("--rebuild", action="store_true", help="with --all: rebuild LADs that already have stats.json")
    b.add_argument("--threads", type=int, default=None, help="DuckDB threads")
    b.add_argument(
        "--memory-limit", default=None, metavar="SIZE", help="DuckDB memory_limit, e.g. 8GB (default 70%% of RAM)"
    )
    b.add_argument("--temp-dir", type=Path, default=None, help="DuckDB temp_directory (default <data-dir>/tmp)")
    b.add_argument("--include-postcode", action="store_true", help="include postcode column (ONS licensing unclear)")

    a_ = sub.add_parser("address", help="local-only full-address table (address_local, never published)")
    _lad_args(a_)

    pv = sub.add_parser("provenance", help="provenance / QA sidecar per UPRN (provenance.parquet)")
    pv.add_argument("--download", action="store_true", help="first fetch Open USRN, Code-Point Open, Open Roads")
    _lad_args(pv, required=False)

    po = sub.add_parser("poi", help="Overture Places attached to UPRNs (poi.parquet)")
    po.add_argument("--download", action="store_true", help="fetch the Overture places release for GB")
    po.add_argument("--assign", action="store_true", help="assign places to LADs (once, after --download)")
    _lad_args(po, required=False)

    ac = sub.add_parser("accuracy", help="check resolved_open outlines against OSM buildings tagged with a UPRN")
    ac.add_argument("--release", required=True, help="assembled release folder name, e.g. 2026-10")
    ac.add_argument("--stage-dir", type=Path, default=None, help="default: <data-dir>/release")
    ac.add_argument(
        "--out", type=Path, default=None, help="summary JSON (default <data-dir>/validation/accuracy_<R>.json)"
    )
    ac.add_argument("--lad", action="append", default=None, help="only this LAD (repeatable)")
    ac.add_argument("--rebuild", action="store_true", help="redo the preparation and every LAD")

    pc_ = sub.add_parser("parcels", help="land parcel polygons per LAD (parcels.parquet, OGL: HMLR INSPIRE / RoS)")
    _lad_args(pc_)

    o = sub.add_parser("osm", help="OSM street/address companion table (streets_odbl.parquet, ODbL)")
    o.add_argument("--convert", action="store_true", help="first convert the PBF to national Parquet")
    o.add_argument("--pbf", type=Path, default=None, help="OSM PBF for --convert")
    _lad_args(o, required=False)

    pp = sub.add_parser("ppd", help="HMLR Price Paid: ppd_open (OGL) and ppd_local (local only) per LAD (E&W)")
    pp.add_argument("--download", action="store_true", help="first fetch pp-complete.csv and convert to Parquet")
    _lad_args(pp, required=False)

    ol = sub.add_parser("outlines", help="finer building outlines: OSM (ODbL) and/or Microsoft (CDLA-Permissive)")
    ol.add_argument("--source", choices=["osm", "ms", "both"], default="both")
    ol.add_argument("--convert", action="store_true", help="first convert the PBF (osm) / UK tiles (ms)")
    ol.add_argument("--pbf", type=Path, default=None, help="OSM PBF for --convert --source osm")
    _lad_args(ol)

    r = sub.add_parser("resolve", help="resolved_open.parquet and resolved_local.parquet per LAD")
    _lad_args(r)
    r.add_argument("--include-postcode", action="store_true", help="include postcode in resolved_open")

    n = sub.add_parser("national", help="national roll-up and HTML report (out/national/)")
    n.add_argument("--report", type=Path, default=None, help="default: <data-dir>/out/national/national.html")

    v = sub.add_parser("validate", help="cross-check every output, write report.html + results.json")
    v.add_argument("--lad", action="append", default=None, help="restrict to LAD (repeatable)")
    v.add_argument("--report-dir", type=Path, default=None, help="default: <data-dir>/validation")

    q = sub.add_parser("qa", help="write an HTML QA report for built outputs")
    q.add_argument("--lad", required=True)
    q.add_argument("--report", type=Path, default=None, help="report path (default: <data-dir>/out/lad=<lad>/qa.html)")

    pb = sub.add_parser("publish", help="assemble + validate a release and upload it (dry run unless --execute)")
    pb.add_argument("--release", required=True, help="release folder name, e.g. 2026-10")
    pb.add_argument("--dest", required=True, help="s3://<account>/<product>/")
    pb.add_argument("--execute", action="store_true", help="really upload (default: dry run)")
    pb.add_argument("--stage-dir", type=Path, default=None, help="default: <data-dir>/release")
    pb.add_argument(
        "--allow",
        nargs="+",
        default=[],
        metavar="COL",
        help="column names exempt from the address denylist (e.g. postcode)",
    )
    pb.add_argument("--force", action="store_true", help="delete and rebuild an existing release folder")
    pb.add_argument(
        "--full", action="store_true", help="ship every table (default: the lean release, publish.LEAN_TABLES)"
    )
    pb.add_argument("--per-lad", action="store_true", help="one file per table per LAD (default: one file per table)")
    _release_extra_args(pb)

    re_ = sub.add_parser(
        "release-extras",
        help="(re)build stac/, CHANGELOG, national files, tiles/ then release_manifest.json and latest.json "
        "for an assembled release",
    )
    re_.add_argument("--release", required=True, help="release folder name, e.g. 2026-10")
    re_.add_argument("--stage-dir", type=Path, default=None, help="default: <data-dir>/release")
    _release_extra_args(re_)
    re_.add_argument("--tiles", action="store_true", help="also build tiles/*.pmtiles (needs tippecanoe, scratch disk)")

    cg = sub.add_parser("changelog", help="UPRN-level diff of two releases -> CHANGELOG.json/.md in the new one")
    cg.add_argument("--old", type=Path, required=True, help="previous release dir")
    cg.add_argument("--new", type=Path, required=True, help="new release dir (CHANGELOG written here)")

    sv = sub.add_parser("stac-validate", help="validate a release's stac/ catalogue and its asset links")
    sv.add_argument("--release", default=None, help="release folder name (under --stage-dir)")
    sv.add_argument("--stage-dir", type=Path, default=None, help="default: <data-dir>/release")
    sv.add_argument("--stac-dir", type=Path, default=None, help="catalogue folder, instead of --release")

    tl = sub.add_parser("tiles", help="PMTiles vector tiles from a release's GeoParquet (needs tippecanoe)")
    tl.add_argument("--release", required=True, help="release folder name")
    tl.add_argument("--stage-dir", type=Path, default=None, help="default: <data-dir>/release")
    tl.add_argument(
        "--kind", default="all", help="tile kind (uprn_resolver.tiles.KINDS), release (the lean release set) or all"
    )
    tl.add_argument("--lad-prefix", default="", help="only LADs starting with this (E09 = London)")
    tl.add_argument("--out-dir", type=Path, default=None, help="default: <release>/tiles")
    tl.add_argument("--tmp-dir", type=Path, default=None, help="scratch dir (default <data-dir>/tmp/tiles)")

    an = sub.add_parser("anomalies", help="source-data anomaly report (data/anomalies/summary.json)")
    an.add_argument("--parquet-dir", type=Path, default=None, help="default: <data-dir>/parquet")
    an.add_argument("--out-dir", type=Path, default=None, help="default: <data-dir>/anomalies")
    an.add_argument("--only", nargs="*", default=None)
    an.add_argument("--doc", type=Path, default=None, help="also write the markdown report here")
    an.add_argument("--doc-only", action="store_true", help="render --doc from an existing summary.json")
    an.add_argument("--memory-limit", default="10GB")

    ni = sub.add_parser("ni", help="Northern Ireland layer from OSM (ODbL, no UPRNs)")
    ns = ni.add_subparsers(dest="ni_command", required=True)
    ne = ns.add_parser("extract", help="cut NI buildings/addresses/highways out of a UK PBF")
    ne.add_argument("--pbf", type=Path, default=None, help="UK PBF (default: newest united-kingdom-*.osm.pbf)")
    ns.add_parser("addresses", help="write out/ni/ni_address_buildings.parquet")

    lk = sub.add_parser("lookup", help="print the resolved row for one UPRN as JSON")
    lk.add_argument("uprn", type=int)
    lk.add_argument(
        "--local",
        action="store_true",
        help="read resolved_local (full address, local use only) instead of resolved_open",
    )

    pl = sub.add_parser("pipeline", help="stage -> build -> extras -> resolve -> national -> validate (no download)")
    pg = pl.add_mutually_exclusive_group(required=True)
    pg.add_argument("--lad", help="LAD code, e.g. E09000019")
    pg.add_argument("--all", action="store_true", help="every GB LAD")
    pl.add_argument("--restage", action="store_true", help="redo staging for LADs already staged")
    pl.add_argument("--rebuild", action="store_true", help="redo build and every later step for finished LADs")
    pl.add_argument("--skip-extras", action="store_true", help="skip provenance/poi/osm/ppd/address/outlines")
    pl.add_argument("--include-postcode", action="store_true")

    sub.add_parser("hmlr-archive", help="download newly published HMLR monthly lookups and keep all months as Parquet")

    rf = sub.add_parser(
        "refresh", help="6-weekly update: download, stage, build, extras, resolve, national, validate, publish dry run"
    )
    rf.add_argument("--release", default=None, help="release folder name for the publish dry run (default: YYYY-MM)")
    rf.add_argument("--dest", default="s3://example/uprn-resolver/", help="publish destination (dry run only)")
    rf.add_argument(
        "--sources",
        nargs="+",
        choices=SOURCES,
        default=None,
        metavar="SRC",
        help=f"download sources (default {' '.join(DEFAULT_SOURCES)}; extras: {' '.join(EXTRA_SOURCES)})",
    )
    rf.add_argument("--with-extras", action="store_true", help="shorthand: add every opt-in source to --sources")
    rf.add_argument("--pbf", type=Path, default=None, help="OSM PBF for osm / osm_buildings")
    rf.add_argument("--skip-download", action="store_true")
    rf.add_argument("--skip-extras", action="store_true")
    rf.add_argument("--restage", action="store_true", help="redo staging/build/resolve for finished LADs")
    rf.add_argument("--include-postcode", action="store_true")
    rf.add_argument("--state-file", type=Path, default=None, help="resume file (default <data-dir>/refresh.state.json)")
    rf.add_argument("--fresh", action="store_true", help="ignore the resume file and run every step")

    al = sub.add_parser("all", help="download, stage, build and QA one LAD")
    al.add_argument("--lad", required=True)
    al.add_argument("--include-postcode", action="store_true")
    return p


def _dirs(args) -> tuple[Path, Path, Path]:
    base: Path = args.data_dir
    return base / "raw", base / "staged", base / "out"


def _mod(name: str):
    return importlib.import_module(f"uprn_resolver.{name}")


def _optional(what: str, module: str, func: str, *a, **kw) -> None:
    """Run an optional-source step; warn and continue on any failure."""
    try:
        getattr(_mod(module), func)(*a, **kw)
    except Exception as e:
        print(f"warning: {what} failed, continuing: {e}", file=sys.stderr)


def _find_pbf(args, prefer_uk: bool = False) -> Path | None:
    if getattr(args, "pbf", None):
        return args.pbf
    cands = [*args.data_dir.glob("*.osm.pbf"), *(args.data_dir / "raw" / "osm").glob("*.osm.pbf")]
    if prefer_uk:
        cands = [c for c in cands if c.name.startswith("united-kingdom")]
    return max(cands, key=lambda c: (c.name.startswith("united-kingdom"), c.name), default=None)


def _pbf_or_download(args, raw: Path) -> Path:
    return _find_pbf(args) or _mod("osm").download_osm(raw)


def _download(args) -> None:
    raw, _, _ = _dirs(args)
    pq = args.data_dir / "parquet"
    sources = getattr(args, "sources", None) or DEFAULT_SOURCES
    if "ostn15" in sources:  # first: the OSM / Overture / Microsoft conversions need the grid
        _optional("OSTN15 grid fetch", "bng", "fetch_grid")
    if "core" in sources:
        if getattr(args, "areas", None):
            import os

            os.environ["UPRN_RESOLVER_AREAS"] = ",".join(args.areas)
        _mod("download").download(getattr(args, "datasets", None), raw)
    for src, mod, fn in (
        ("hmlr", "hmlr", "download_hmlr"),
        ("streets", "streets", "download_streets"),
        ("epc", "epc", "download_epc"),
        ("inspire", "inspire", "download_inspire"),
        ("ros", "ros", "download_ros"),
        ("provenance", "provenance", "download_provenance_sources"),
    ):
        if src in sources:
            _optional(f"{src} download", mod, fn, raw)
    if "osm" in sources:
        _optional("OSM download", "osm", "convert_osm", _pbf_or_download(args, raw), pq)
    if "osm_buildings" in sources:
        _optional("OSM buildings download", "osm_buildings", "convert_osm_buildings", _pbf_or_download(args, raw), pq)
    if "msbuildings" in sources:
        try:
            m = _mod("msbuildings")
            m.convert_all_tiles(m.fetch_links(raw / "msbuildings_links" / "dataset-links.csv"), pq, raw / "msbuildings")
        except Exception as e:
            print(f"warning: msbuildings download failed, continuing: {e}", file=sys.stderr)
    if "overture" in sources:
        _optional("Overture places download", "poi", "download_places", pq)


def _stage(args) -> int:
    raw, staged, _ = _dirs(args)
    st = _mod("stage")

    if getattr(args, "all", False):
        res = st.stage_all(raw, staged, skip_existing=not args.restage, include_epc=not args.no_epc)
        print(f"Staged {len(res['staged'])}, skipped {len(res['skipped'])}, failed {len(res['failed'])}")
        if res["ignored"]:
            print(f"  ignored non-GB/odd LAD codes: {res['ignored']}")
        for lad, msg in res["failed"].items():
            print(f"  FAILED {lad}: {msg}", file=sys.stderr)
        return 1 if res["failed"] else 0
    st.stage(args.lad, raw, staged)
    _optional("HMLR staging", "hmlr", "stage_hmlr", args.lad, raw, staged)
    _optional("streets staging", "streets", "stage_streets", args.lad, raw, staged)
    _optional("EPC staging", "epc", "stage_epc", args.lad, raw, staged)
    _optional("INSPIRE staging", "inspire", "stage_inspire", args.lad, raw, staged)
    _optional("ROS staging", "ros", "stage_ros", args.lad, raw, staged)
    return 0


def _build(args) -> int:
    _, staged, out = _dirs(args)
    bd = _mod("build")

    opts = {
        k: v
        for k, v in (
            ("threads", getattr(args, "threads", None)),
            ("memory_limit", getattr(args, "memory_limit", None)),
            ("temp_directory", getattr(args, "temp_dir", None)),
        )
        if v is not None
    }
    pc = getattr(args, "include_postcode", False)
    if getattr(args, "all", False):
        res = bd.build_all(staged, out, skip_existing=not args.rebuild, include_postcode=pc, **opts)
        return _summary("Built", res)
    stats = bd.build(args.lad, staged, out, include_postcode=pc, **opts)
    print_stats(stats)
    return 0


def _summary(verb: str, res: dict) -> int:
    """Print 'Verb n, skipped n, failed n' for a *_all result; return 1 if anything failed."""
    done = next((res[k] for k in ("built", "done", "converted") if k in res), [])
    failed = res.get("failed") or {}
    print(f"{verb} {len(done)}, skipped {len(res.get('skipped', []))}, failed {len(failed)}")
    for lad, msg in failed.items() if isinstance(failed, dict) else ((x, "") for x in failed):
        print(f"  FAILED {lad}: {msg}", file=sys.stderr)
    not_staged = res.get("not_staged") or []
    if not_staged:
        print(f"  NOT STAGED (no .stage_done, skipped): {', '.join(not_staged)}", file=sys.stderr)
    warnings = res.get("warnings") or {}
    for lad, msg in warnings.items() if isinstance(warnings, dict) else ((x, "") for x in warnings):
        print(f"  WARNING {lad}: {msg}", file=sys.stderr)
    return 1 if failed else 0


def _per_lad(args, label: str, module: str, one: str, many: str, *extra, **kw) -> int:
    """--lad X -> module.one(lad, staged, out, parquet); --all -> module.many(staged, out, parquet)."""
    _, staged, out = _dirs(args)
    pq = args.data_dir / "parquet"
    m = _mod(module)
    if getattr(args, "all", False):
        return _summary(label, getattr(m, many)(staged, out, pq, *extra, skip_existing=not args.rebuild, **kw))
    print_stats(getattr(m, one)(args.lad, staged, out, pq, **kw) or {})
    return 0


def _address(args) -> int:
    return _per_lad(args, "Built", "address", "address_local", "address_local_all")


def _accuracy(args) -> int:
    res = _mod("accuracy").run(
        _dirs(args)[2],  # the pipeline's per-LAD output (data/out): independent of the release layout and names
        args.data_dir / "parquet",
        args.data_dir / "tmp" / "accuracy",
        args.out or args.data_dir / "validation" / f"accuracy_{args.release}.json",
        lads=args.lad,
        skip_existing=not args.rebuild,
        release_name=args.release,
    )
    for g in res["summary"]["groups"]:
        if g["source"] is None and g["building"] == "single":
            print(f"  {g['confidence']:<12} n={g['n']:>9,}  inside {g['inside_pct']}%  exclusive {g['exclusive_pct']}%")
    if res["failed"]:
        print(f"failed: {res['failed']}", file=sys.stderr)
    return 1 if res["failed"] else 0


def _parcels(args) -> int:
    return _per_lad(args, "Built", "parcels", "parcels_for_lad", "parcels_all")


def _provenance(args) -> int:
    if args.download:
        _mod("provenance").download_provenance_sources(args.data_dir / "raw")
    if not (args.lad or args.all):
        return 0
    return _per_lad(args, "Built", "provenance", "provenance_for_lad", "provenance_all")


def _poi(args) -> int:
    pq = args.data_dir / "parquet"
    m = _mod("poi")
    if not (args.download or args.assign or args.lad or args.all):
        raise SystemExit("poi: give --download, --assign and/or --lad X | --all")
    if args.download:
        m.download_places(pq)
    if args.assign:
        print_stats(m.assign_places_lad(pq) or {})
    if not (args.lad or args.all):
        return 0
    return _per_lad(args, "Done", "poi", "poi_for_lad", "poi_all")


def _osm(args) -> int:
    if args.convert:
        pbf = _find_pbf(args)
        if pbf is None:
            raise SystemExit("osm --convert: no PBF found; pass --pbf or run download --sources osm")
        print_stats(_mod("osm").convert_osm(pbf, args.data_dir / "parquet") or {})
    if not (args.lad or args.all):
        return 0
    return _per_lad(args, "Done", "osm", "osm_for_lad", "osm_all")


def _ppd(args) -> int:
    if args.download:
        print_stats(_mod("ppd").download_ppd(args.data_dir / "parquet") or {})
    if not (args.lad or args.all):
        if not args.download:
            raise SystemExit("ppd: give --download and/or --lad X | --all")
        return 0
    return _per_lad(args, "Done", "ppd", "ppd_lad", "ppd_all")


def _outlines(args) -> int:
    rc = 0
    if args.convert:
        pq = args.data_dir / "parquet"
        if args.source in ("osm", "both"):
            pbf = _find_pbf(args)
            if pbf is None:
                raise SystemExit("outlines --convert: no PBF found; pass --pbf")
            print_stats(_mod("osm_buildings").convert_osm_buildings(pbf, pq) or {})
        if args.source in ("ms", "both"):
            m = _mod("msbuildings")
            raw = args.data_dir / "raw"
            links = m.fetch_links(raw / "msbuildings_links" / "dataset-links.csv")
            m.convert_all_tiles(links, pq, raw / "msbuildings")
    if args.source in ("osm", "both"):
        rc |= _per_lad(args, "Done", "osm_buildings", "osm_outline_for_lad", "osm_outline_all")
    if args.source in ("ms", "both"):
        rc |= _per_lad(args, "Built", "msbuildings", "ms_outline_for_lad", "ms_outline_all")
    return rc


def _resolve(args) -> int:
    _, staged, out = _dirs(args)
    m = _mod("resolve")
    if args.all:
        return _summary(
            "Done",
            m.resolve_all(
                out, include_postcode=args.include_postcode, skip_existing=not args.rebuild, staged_dir=staged
            ),
        )
    print_stats(m.resolve_lad(args.lad, out, args.include_postcode, staged_dir=staged) or {})
    return 0


def _national(args) -> int:
    _, _, out = _dirs(args)
    report = args.report or out / "national" / "national.html"
    res = _mod("national").national(out, report)
    print(f"National report: {report}")
    print_stats({k: v for k, v in (res or {}).items() if not isinstance(v, (dict, list))})
    return 0


def _validate(args) -> int:
    _, staged, out = _dirs(args)
    rep = args.report_dir or args.data_dir / "validation"
    res = _mod("validate").validate(out, staged, args.data_dir / "parquet", rep, args.lad)
    print(res["status"], res["summary"], f"report: {rep / 'report.html'}")
    return 1 if res["status"] == "FAIL" else 0


def _qa(args) -> None:
    _, _, out = _dirs(args)
    qa = _mod("qa")

    report = getattr(args, "report", None) or out / f"lad={args.lad}" / "qa.html"
    summary = qa.qa(args.lad, out, report)
    print(f"QA report: {summary['report_path']}")
    print_stats({k: v for k, v in summary.items() if k != "suspicious"})


def _publish(args) -> int:
    _, _, out = _dirs(args)
    stage = args.stage_dir or args.data_dir / "release"
    _mod("publish").publish(
        out,
        args.release,
        args.dest,
        dry_run=not args.execute,
        stage_dir=stage,
        allow=tuple(args.allow),
        **_release_kwargs(args),
    )
    return 0


def _release_kwargs(args) -> dict:
    kw = {
        "stac": not getattr(args, "no_stac", False),
        "national_files": getattr(args, "national_files", False),
        "changelog_against": getattr(args, "changelog_against", None),
        "source_manifest": getattr(args, "source_manifest", None),
    }
    if hasattr(args, "force"):
        kw["force"] = args.force
    kw["lean"] = not getattr(args, "full", False)
    kw["single_file"] = not getattr(args, "per_lad", False)
    return kw


def _release_root(args) -> Path:
    return (args.stage_dir or args.data_dir / "release") / args.release


def _release_extras(args) -> int:
    """Add the extras to an already assembled release, then rewrite release_manifest.json (so it lists stac/,
    tiles/, ... too) and latest.json."""
    root = _release_root(args)
    if not ((root / "uprn").exists() or (root / "uprn_links").exists()):
        raise SystemExit(f"release-extras: {root} is not an assembled release (no uprn/ or uprn_links/)")
    pub = _mod("publish")
    if args.national_files:
        pub.write_national(root)
    if args.changelog_against:
        pub.diff_releases(args.changelog_against, root, write=True)
    rc = 0
    if not args.no_stac:
        res = _mod("stac").build_stac(root, args.release)
        if res.get("invalid") or res.get("missing_assets"):
            print(f"STAC validation failed: {res.get('invalid') or res['missing_assets'][:5]}", file=sys.stderr)
            rc = 1
    if args.tiles:
        extra = {"kind": "release", "lad_prefix": "", "out_dir": None, "tmp_dir": None}
        _tiles(argparse.Namespace(**{**vars(args), **extra}))
    acc = args.data_dir / "validation" / f"accuracy_{args.release}.json"
    if acc.exists():  # written by `accuracy`, which needs the assembled release
        shutil.copy2(acc, root / "accuracy.json")
    pub.write_manifest(root, args.release, source_manifest=args.source_manifest)
    pub.write_latest(root.parent, args.release)
    print(f"Release extras written for {root}")
    return rc


def _changelog(args) -> int:
    res = _mod("publish").diff_releases(args.old, args.new, write=True)
    for t, e in res["tables"].items():
        print(f"  {t}: " + (e["status"] if "status" in e else f"+{e['added']} -{e['removed']}"))
    print(f"Wrote {args.new / 'CHANGELOG.json'} and CHANGELOG.md")
    return 0


def _stac_validate(args) -> int:
    if args.stac_dir:
        stac = args.stac_dir
    elif args.release:
        stac = _release_root(args) / "stac"
    else:
        raise SystemExit("stac-validate: give --release or --stac-dir")
    res = _mod("stac").validate_stac(stac)
    print(json.dumps(res, indent=2, default=str))
    return 1 if res.get("invalid") or res.get("missing_assets") else 0


def _tiles(args) -> int:
    t = _mod("tiles")
    kinds = list(t.KINDS) if args.kind == "all" else list(t.RELEASE_KINDS) if args.kind == "release" else [args.kind]
    if args.kind not in ("all", "release") and args.kind not in t.KINDS:
        raise SystemExit(f"tiles: unknown --kind {args.kind!r}; choose from {', '.join(t.KINDS)}, release or all")
    tmp = args.tmp_dir or args.data_dir / "tmp" / "tiles"
    for k in kinds:
        res = t.build(_release_root(args), k, prefix=args.lad_prefix, out_dir=args.out_dir, tmp_dir=tmp)
        print(json.dumps(res), flush=True)
    return 0


def _anomalies(args) -> int:
    argv = [
        "--parquet-dir", str(args.parquet_dir or args.data_dir / "parquet"),
        "--out-dir", str(args.out_dir or args.data_dir / "anomalies"),
        "--memory-limit", args.memory_limit,
    ]  # fmt: skip
    if args.only is not None:
        argv += ["--only", *args.only]
    if args.doc:
        argv += ["--doc", str(args.doc)]
    if args.doc_only:
        argv.append("--doc-only")
    _mod("anomalies").main(argv)
    return 0


def _ni(args) -> int:
    pq = args.data_dir / "parquet"
    m = _mod("ni")
    if args.ni_command == "extract":
        pbf = _find_pbf(args, prefer_uk=True)
        if pbf is None:
            raise SystemExit("ni extract: no UK PBF found (the GB extract has no NI); pass --pbf")
        print_stats(m.extract_ni(pbf, pq) or {})
    else:
        print_stats(m.ni_address_points(pq, args.data_dir / "out") or {})
    return 0


def _lookup(args) -> int:
    _, _, out = _dirs(args)
    row = _mod("resolve").lookup(args.uprn, out, local=args.local)
    if row is None:
        print(f"UPRN {args.uprn} not found", file=sys.stderr)
        return 1
    print(json.dumps(row, indent=2, default=str))
    return 0


def _pipeline(args) -> int:
    """stage -> build -> extras -> resolve -> national -> validate. Extras and national warn and continue."""
    ns = argparse.Namespace
    one = bool(args.lad)
    sel = {"lad": args.lad, "all": not one}
    rc = 0
    rc |= _stage(ns(**vars(args), **{"no_epc": False}))
    rc |= _build(ns(data_dir=args.data_dir, rebuild=args.rebuild, include_postcode=args.include_postcode, **sel))
    if not args.skip_extras:
        for name, fn, extra in (
            ("provenance", _provenance, {"download": False}),
            ("poi", _poi, {"download": False, "assign": False}),
            ("osm", _osm, {"convert": False}),
            ("ppd", _ppd, {"download": False}),
            ("address", _address, {}),
            ("outlines", _outlines, {"convert": False, "source": "both"}),
            ("parcels", _parcels, {}),
        ):
            try:
                if fn(ns(data_dir=args.data_dir, rebuild=args.rebuild, **sel, **extra)):
                    print(f"warning: {name} had failures, continuing", file=sys.stderr)
            except Exception as e:
                print(f"warning: {name} failed, continuing: {e}", file=sys.stderr)
    rc |= _resolve(ns(data_dir=args.data_dir, rebuild=args.rebuild, include_postcode=args.include_postcode, **sel))
    try:
        _national(ns(data_dir=args.data_dir, report=None))
    except Exception as e:
        print(f"warning: national roll-up failed, continuing: {e}", file=sys.stderr)
    rc |= _validate(ns(data_dir=args.data_dir, lad=[args.lad] if one else None, report_dir=None))
    return rc


def _hmlr_archive(args) -> int:
    res = _mod("hmlr").archive_hmlr(args.data_dir / "raw")
    print(f"HMLR archive: {len(res['new'])} new, {len(res['archived'])} months held")
    for n in res["new"]:
        print(f"  new: {n}")
    for n in res["missing"]:
        print(f"  missing: {n}", file=sys.stderr)
    return 0


def _refresh(args) -> int:
    """One command for the 6-weekly update. Resumable: completed steps are recorded in the state file and skipped
    on a re-run (per-LAD steps are additionally resumable on their own). Failed steps are reported in the summary;
    the run continues, and exit status is 1 if any step failed. Publish is always a dry run."""
    import datetime as dt

    state_path = args.state_file or args.data_dir / "refresh.state.json"
    release = args.release or dt.date.today().strftime("%Y-%m")
    state = {}
    if not args.fresh and state_path.exists():
        try:
            state = json.loads(state_path.read_text())
        except ValueError:
            state = {}
    if state.get("release") != release:
        state = {"release": release, "done": {}}
    done = state["done"]
    sources = list(args.sources or DEFAULT_SOURCES)
    if args.with_extras:
        sources += [s for s in EXTRA_SOURCES if s not in sources]
    ns = argparse.Namespace
    sel = {"lad": None, "all": True}
    rebuild = args.restage
    pc = args.include_postcode
    steps: list[tuple[str, object]] = []
    if not args.skip_download:
        dl = ns(**{**vars(args), "sources": sources, "datasets": None, "areas": None})
        steps.append(("download", lambda: _download(dl) or 0))
        steps.append(("hmlr-archive", lambda: _hmlr_archive(args)))
    steps += [
        ("stage", lambda: _stage(ns(data_dir=args.data_dir, all=True, lad=None, restage=args.restage, no_epc=False))),
        ("build", lambda: _build(ns(data_dir=args.data_dir, rebuild=rebuild, include_postcode=pc, **sel))),
    ]
    if not args.skip_extras:
        for name, fn, extra in (
            ("provenance", _provenance, {"download": False}),
            ("poi", _poi, {"download": False, "assign": False}),
            ("osm", _osm, {"convert": False}),
            ("ppd", _ppd, {"download": False}),
            ("address", _address, {}),
            ("outlines", _outlines, {"convert": False, "source": "both"}),
            ("parcels", _parcels, {}),
        ):
            steps.append(
                (name, lambda fn=fn, extra=extra: fn(ns(data_dir=args.data_dir, rebuild=rebuild, **sel, **extra)))
            )
    steps += [
        ("resolve", lambda: _resolve(ns(data_dir=args.data_dir, rebuild=rebuild, include_postcode=pc, **sel))),
        ("national", lambda: _national(ns(data_dir=args.data_dir, report=None))),
        ("validate", lambda: _validate(ns(data_dir=args.data_dir, lad=None, report_dir=None))),
        (
            "publish",
            lambda: _publish(
                ns(data_dir=args.data_dir, release=release, dest=args.dest, execute=False, stage_dir=None, allow=[])
            ),
        ),
    ]
    results: dict[str, str] = {}
    for name, fn in steps:
        if name in done:
            results[name] = "skipped (already done)"
            continue
        try:
            rc = fn()
        except Exception as e:  # noqa: BLE001 - report and carry on
            results[name] = f"FAILED: {e}"
            print(f"warning: refresh step {name} failed: {e}", file=sys.stderr)
            continue
        if rc:
            results[name] = f"FAILED (exit {rc})"
        else:
            results[name] = "ok"
            done[name] = dt.datetime.now().isoformat(timespec="seconds")
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(json.dumps(state, indent=2))
    print(f"Refresh summary (release {release})")
    for name, r in results.items():
        print(f"  {name}: {r}")
    failed = [n for n, r in results.items() if r.startswith("FAILED")]
    if not failed and state_path.exists():
        state_path.unlink()  # complete: next refresh starts clean
    return 1 if failed else 0


def print_stats(stats) -> None:
    print("Build summary")
    if not stats:
        print("  (no stats returned)")
        return
    for k, v in stats.items():
        print(f"  {k}: {v}")


_COMMANDS = {
    "stage": _stage,
    "build": _build,
    "address": _address,
    "provenance": _provenance,
    "poi": _poi,
    "osm": _osm,
    "ppd": _ppd,
    "outlines": _outlines,
    "parcels": _parcels,
    "accuracy": _accuracy,
    "resolve": _resolve,
    "national": _national,
    "validate": _validate,
    "publish": _publish,
    "release-extras": _release_extras,
    "changelog": _changelog,
    "stac-validate": _stac_validate,
    "tiles": _tiles,
    "anomalies": _anomalies,
    "ni": _ni,
    "lookup": _lookup,
    "pipeline": _pipeline,
    "hmlr-archive": _hmlr_archive,
    "refresh": _refresh,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cmd = args.command
    if cmd == "download":
        _download(args)
    elif cmd == "qa":
        _qa(args)
    elif cmd == "all":
        _download(argparse.Namespace(**{**vars(args), "datasets": None, "areas": None, "sources": None}))
        _stage(args)
        _build(args)
        _qa(args)
    else:
        return _COMMANDS[cmd](args) or 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
