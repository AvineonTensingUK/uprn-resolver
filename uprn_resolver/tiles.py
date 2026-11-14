"""PMTiles archives (tippecanoe) for web viewing: UPRN points and building outlines, one archive per licence.

Toolchain: ``tippecanoe`` (Homebrew) reads GeoJSONSeq from stdin and writes PMTiles directly. This module streams the
release LAD by LAD (DuckDB -> OSTN15 inverse via :mod:`uprn_resolver.bng` -> GeoJSONSeq) straight into one tippecanoe
process, so no per-LAD intermediate files hit the disk (tippecanoe's own work files go under ``tmp_dir``).
Licences are never mixed: each archive is built from exactly one release table and carries its licence in the
PMTiles metadata ``attribution``. Check with ``pmtiles show`` / ``pmtiles verify``.

    uv run python -m uprn_resolver.tiles --release data/release/2026-10 --kind points --lad-prefix E09000019
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import duckdb
import numpy as np
import shapely

from uprn_resolver import bng

MIN_FREE_BYTES = 6 * 1024**3
PRECISION = 7  # decimal degrees (~1 cm)


@dataclass(frozen=True)
class Kind:
    archive: str
    table: str
    layer: str
    props: tuple[str, ...]  # output property names (SQL expressions are in SQL below)
    sql: str  # SELECT ... FROM {path}; geometry -> WKB as ``wkb`` (or x, y for points)
    zooms: tuple[int, int]
    attribution: str
    description: str
    point: bool = False
    lonlat: bool = False  # x, y are already WGS84 lon/lat (no OSTN15 inverse)
    files: str = ""  # glob relative to the data dir, for tables outside the release (no LAD partitions)


OGL_ATTR = (
    "Contains OS data © Crown copyright and database right 2026. OGL v3.0. "
    "Microsoft-derived fields CDLA-Permissive-2.0; Overture POI not included."
)
KINDS: dict[str, Kind] = {
    "points": Kind(
        "uprn_points.pmtiles",
        "resolved_open",
        "uprn",
        ("uprn", "outline_confidence", "best_outline_source", "street_name", "locality"),
        "SELECT uprn, outline_confidence, best_outline_source, street_name, locality, x, y "
        "FROM read_parquet('{path}') WHERE x IS NOT NULL AND y IS NOT NULL",
        (5, 16),
        OGL_ATTR,
        "Open UPRN points (resolved_open): OGL v3.0 core",
        point=True,
    ),
    "ogl": Kind(
        "outlines_ogl.pmtiles",
        "buildings",
        "outlines",
        ("building_id",),
        "SELECT building_id, ST_AsWKB(geometry) AS wkb FROM read_parquet('{path}')",
        (13, 16),
        "Contains OS data © Crown copyright and database right 2026. OGL v3.0 (OS OpenMap Local buildings).",
        "OS OpenMap Local building outlines (OGL v3.0)",
    ),
    "parcels": Kind(
        "parcels.pmtiles",
        "parcels",
        "parcels",
        ("parcel_id", "parcel_source", "uprns_in_parcel", "parcel_area_m2"),
        "SELECT parcel_id, parcel_source, uprns_in_parcel, round(parcel_area_m2, 1) AS parcel_area_m2, "
        "ST_AsWKB(geometry) AS wkb FROM read_parquet('{path}')",
        (13, 16),
        "This information is subject to Crown copyright and database rights 2026 and is reproduced with the permission "
        "of HM Land Registry. The polygons (including the associated geometry, namely x, y co-ordinates) are subject "
        "to Crown copyright and database rights 2026 Ordnance Survey AC0000851063. Scotland: © Crown copyright. "
        "Reproduced with the permission of Registers of Scotland. Contains OS data © Crown copyright and database "
        "right 2026. OGL v3.0.",
        "Land parcel polygons (OGL v3.0: HM Land Registry INSPIRE, Registers of Scotland)",
    ),
    "ms": Kind(
        "outlines_ms.pmtiles",
        "outline_ms_buildings",
        "outlines",
        ("ms_building_id", "area_m2"),
        "SELECT ms_building_id, round(area_m2, 1) AS area_m2, ST_AsWKB(geometry) AS wkb FROM read_parquet('{path}')",
        (13, 16),
        "Building footprints © Microsoft, CDLA-Permissive-2.0 (GlobalMLBuildingFootprints).",
        "Microsoft building footprints (CDLA-Permissive-2.0)",
    ),
    "odbl": Kind(
        "outlines_odbl.pmtiles",
        "outline_odbl_buildings",
        "outlines",
        ("osm_building_id", "osm_building_tag", "osm_building_area_m2"),
        "SELECT osm_building_id, osm_building_tag, round(osm_building_area_m2, 1) AS osm_building_area_m2, "
        "ST_AsWKB(geometry) AS wkb FROM read_parquet('{path}')",
        (13, 16),
        "© OpenStreetMap contributors, ODbL-1.0 (share-alike). This archive is a Derivative Database: keep it separate "
        "from OGL/CDLA data.",
        "OpenStreetMap building outlines (ODbL-1.0, share-alike)",
    ),
    "ni": Kind(
        "ni_addresses.pmtiles",
        "ni_address_buildings",
        "ni_address",
        ("osm_id", "street", "city", "confidence", "building_match"),
        "SELECT osm_id, street, city, confidence, building_match, lon AS x, lat AS y "
        "FROM read_parquet('{path}') WHERE lon IS NOT NULL AND lat IS NOT NULL",
        (5, 16),
        "© OpenStreetMap contributors, ODbL-1.0 (share-alike). Northern Ireland OSM addresses, no UPRNs: keep "
        "separate from OGL/CDLA data.",
        "Northern Ireland OSM address points (ODbL-1.0, share-alike)",
        point=True,
        lonlat=True,
        files="out/ni/ni_address_buildings.parquet",
    ),
}


def lad_files(release: Path, table: str, prefix: str = "") -> list[Path]:
    return sorted(release.glob(f"{table}/lad={prefix}*/*.parquet"))


def kind_files(release: Path, kind: Kind, prefix: str = "") -> list[Path]:
    """Source files for one kind: the release's LAD partitions, or ``kind.files`` under the data dir (the release's
    grandparent, ``data/release/<R>``), which has no LADs so ``prefix`` is ignored."""
    if kind.files:
        return sorted(release.resolve().parents[1].glob(kind.files))
    return lad_files(release, kind.table, prefix)


def free_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


def check_disk(path: Path) -> None:
    if free_bytes(path) < MIN_FREE_BYTES:
        raise RuntimeError(f"free disk under {MIN_FREE_BYTES / 1024**3:.0f} GB at {path}; aborting")


def _con() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")
    return con


def _props(names: tuple[str, ...], row: tuple) -> dict:
    out = {}
    for n, v in zip(names, row, strict=False):
        if v is not None:
            out[n] = round(float(v), 1) if isinstance(v, (float, Decimal)) else v
    return out


def _snap(geoms: np.ndarray) -> np.ndarray:
    """Round to PRECISION decimals. Invalid source polygons (seen in Microsoft footprints) can make GEOS raise a
    TopologyException, so repair them first and, failing that, round per geometry keeping the original on error."""
    grid = 10.0**-PRECISION
    geoms = np.where(shapely.is_valid(geoms), geoms, shapely.make_valid(geoms))
    try:
        return shapely.set_precision(geoms, grid)
    except shapely.errors.GEOSException:
        out = geoms.copy()
        for i, g in enumerate(geoms):
            try:
                out[i] = shapely.set_precision(g, grid)
            except shapely.errors.GEOSException:
                pass
        return out


def features(kind: Kind, path: Path, con: duckdb.DuckDBPyConnection | None = None) -> Iterator[str]:
    """GeoJSON Feature lines (WGS84) for one LAD file."""
    con = con or _con()
    cur = con.execute(kind.sql.format(path=str(path).replace("'", "''")))
    n = len(kind.props)
    while rows := cur.fetchmany(50_000):
        if kind.point:
            xs = np.array([r[n] for r in rows], dtype="float64")
            ys = np.array([r[n + 1] for r in rows], dtype="float64")
            lon, lat = (xs, ys) if kind.lonlat else bng.bng_to_wgs84(xs, ys)
            for r, lo, la in zip(rows, lon, lat, strict=True):
                geom = {"type": "Point", "coordinates": [round(float(lo), PRECISION), round(float(la), PRECISION)]}
                yield json.dumps({"type": "Feature", "properties": _props(kind.props, r[:n]), "geometry": geom})
        else:
            rows = [r for r in rows if r[n] is not None]
            geoms = shapely.from_wkb(np.array([bytes(r[n]) for r in rows], dtype=object))
            geoms = bng.transform_geometries(geoms, inverse=True)
            gj = shapely.to_geojson(_snap(geoms))
            for r, g in zip(rows, gj, strict=True):
                yield f'{{"type":"Feature","properties":{json.dumps(_props(kind.props, r[:n]))},"geometry":{g}}}'


def tippecanoe_cmd(kind: Kind, out: Path, tmp_dir: Path) -> list[str]:
    zmin, zmax = kind.zooms
    cmd = [
        "tippecanoe", "-o", str(out), "--force", "-l", kind.layer, "-Z", str(zmin), "-z", str(zmax),
        "-t", str(tmp_dir), "-n", kind.description.split(" (")[0], "-N", kind.description,
        "-A", kind.attribution, "--read-parallel", "-q",
    ]  # fmt: skip
    if kind.point:
        # low zooms: thin points evenly (keeps density pattern); full detail at max zoom
        cmd += ["--drop-densest-as-needed", "--extend-zooms-if-still-dropping", "--no-tile-size-limit"]
    else:
        cmd += ["--drop-densest-as-needed", "--coalesce-densest-as-needed", "--simplification=4"]
    return cmd


def build(
    release: Path, kind_name: str, *, prefix: str = "", out_dir: Path | None = None, tmp_dir: Path | None = None
) -> dict:
    kind = KINDS[kind_name]
    out_dir = out_dir or release / "tiles"
    tmp_dir = (tmp_dir or Path("data/tmp/tiles")).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    files = kind_files(release, kind, prefix)
    if not files:
        raise FileNotFoundError(f"no {kind.table} files for prefix {prefix!r} under {release}")
    prefix = "" if kind.files else prefix
    check_disk(tmp_dir)
    out = out_dir / (kind.archive if not prefix else kind.archive.replace(".pmtiles", f"_{prefix}.pmtiles"))
    part = out.with_name(out.stem + ".partial.pmtiles")  # tippecanoe picks the format from the extension
    t0 = time.time()
    proc = subprocess.Popen(tippecanoe_cmd(kind, part, tmp_dir), stdin=subprocess.PIPE, text=True, bufsize=1 << 20)
    con, n = _con(), 0
    try:
        assert proc.stdin is not None
        for i, f in enumerate(files):
            if i % 10 == 0:
                check_disk(tmp_dir)
            for line in features(kind, f, con):
                proc.stdin.write(line)
                proc.stdin.write("\n")
                n += 1
        proc.stdin.close()
        if proc.wait() != 0:
            raise RuntimeError(f"tippecanoe exited {proc.returncode}")
    except BaseException:
        proc.kill()
        part.unlink(missing_ok=True)
        raise
    part.replace(out)
    return {
        "archive": str(out),
        "features": n,
        "lads": len(files),
        "bytes": out.stat().st_size,
        "seconds": round(time.time() - t0, 1),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--release", type=Path, default=Path("data/release/2026-10"))
    ap.add_argument("--kind", choices=[*KINDS, "all"], default="all")
    ap.add_argument("--lad-prefix", default="", help="only LADs starting with this (E09 = London, E09000019 = one)")
    ap.add_argument("--out-dir", type=Path)
    ap.add_argument("--tmp-dir", type=Path, default=Path("data/tmp/tiles"))
    a = ap.parse_args(argv)
    for k in KINDS if a.kind == "all" else [a.kind]:
        print(json.dumps(build(a.release, k, prefix=a.lad_prefix, out_dir=a.out_dir, tmp_dir=a.tmp_dir)), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
