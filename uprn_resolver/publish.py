"""Write, validate and (optionally) upload a cloud-native GeoParquet release to Source Cooperative.

The release writer re-sorts each LAD table along a Hilbert curve, adds a per-row ``bbox`` covering column,
writes row groups of ~100k rows (zstd) and GeoParquet 1.1.0 ``geo`` metadata with ``covering``; see docs/format.md.

Upload uses the AWS CLI against the Source data proxy (https://data.source.coop).
Credentials are taken from the environment (AWS_ACCESS_KEY_ID etc.); see docs/publishing.md.
Dry-run is the default and never touches the network.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import duckdb
import pyarrow.parquet as pq

ENDPOINT = "https://data.source.coop"
PUBLISH_DIR = Path(__file__).resolve().parent.parent / "publish"
FORBIDDEN = re.compile(r"address|street|thoroughfare|building_?name|saon|paon|locality|town|postcode", re.I)
# Open Names / Open Roads derived (OGL) street attributes: not address text, so exempt from FORBIDDEN.
ALLOWED_NAMES = frozenset(
    {
        "street_name",
        "street_name_alt",
        "locality",
        "street_method",
        # provenance.parquet: OGL-derived street/postcode QA fields that are not address or postcode text
        "usrn_street_type",
        "street_name_fallback",
        "street_name_fallback_alt",
        "street_name_fallback_method",
        "street_name_fallback_cover",
        "postcode_centroid_distance_m",
        "postcode_flag",
    }
)
# Columns holding postcode text: dropped from provenance.parquet unless 'postcode' is allowed.
PROVENANCE_POSTCODE_COLS = ("postcode_onsud", "postcode_nearest_cpo", "postcode_nearest_cpo_distance_m")

ROW_GROUP_SIZE = 100_000
COMPRESSION_LEVEL = 9
# British National Grid extent, used to normalise the Hilbert curve (same for every file => stable ordering)
BNG_EXTENT = (0.0, 0.0, 700_000.0, 1_300_000.0)
GEO_TABLES = ("uprn", "buildings")  # required GeoParquet tables: Hilbert sorted + bbox covering
# Optional GeoParquet tables -> column used as Hilbert tie-break
OPTIONAL_GEO = {
    "resolved_open": "uprn",
    "parcels": "parcel_id",
    "outline_ms_buildings": "ms_building_id",
    "outline_odbl_buildings": "osm_building_id",
}
PLAIN_TABLES = (
    "epc", "provenance", "poi", "outline_ms", "outline_odbl", "ppd_open", "ppd_open_summary"
)  # fmt: skip  # plain Parquet keyed by uprn
ODBL_TABLE = "streets_odbl"
# Licence classes. OGL tables are the core; every other table lives in its own folder with a LICENCE.txt.
ODBL_TABLES = (ODBL_TABLE, "outline_odbl", "outline_odbl_buildings")
MS_TABLES = ("outline_ms", "outline_ms_buildings")
OVERTURE_TABLES = ("poi",)
# table -> LAD-level sidecar that supplies the folder's LICENCE.txt (None: OGL core, covered by LICENCE.md)
LICENCE_SIDECAR = {
    "poi": "poi.LICENCE.txt",
    "ppd_open": "ppd_open.LICENCE.txt",
    "ppd_open_summary": "ppd_open.LICENCE.txt",
    "resolved_open": "resolved_open.LICENCE.txt",
    "parcels": "parcels.LICENCE.txt",
    "streets_odbl": "streets_odbl.LICENCE.txt",
    "outline_ms": "outline_ms.LICENCE.txt",
    "outline_ms_buildings": "outline_ms.LICENCE.txt",
    "outline_odbl": "outline_odbl.LICENCE.txt",
    "outline_odbl_buildings": "outline_odbl.LICENCE.txt",
}
# Never released, whatever the caller does (full addresses / postcodes; EPC-restricted and ODbL address text).
NEVER_SHIP = ("address_local", "resolved_local", "ppd_local")  # ppd_local also covers ppd_local_summary
# Content-based counterpart: columns that only exist in the local-only tables (address text, OSM address candidates,
# ODbL/EPC-restricted outline ids). Any released parquet carrying one is rejected whatever its file name.
NEVER_SHIP_COLUMNS = re.compile(
    r"^(single_line_address|address_(source|licence|osm_method|building_level)|osm_address_candidate\w*|\w+_local)$",
    re.I,
)
# Explicit per-table column allowances for the FORBIDDEN name filter. ODbL/Overture/Microsoft tables carry
# their own licences and are exempt; resolved_open's street/locality are OGL OS Open Names/Open Roads values.
TABLE_ALLOW = {
    "resolved_open": ("street_name", "street_source", "locality", "postcode_flag"),
    **{t: ("*",) for t in (*ODBL_TABLES, *MS_TABLES, *OVERTURE_TABLES)},
}
# ODbL content must never appear inside an OGL table: no osm_* columns, no OSM-derived outline source values.
OGL_TABLES = ("uprn", "buildings", "epc", "provenance", "resolved_open", "parcels", "ppd_open", "ppd_open_summary")
ODBL_COLUMN = re.compile(r"^(osm_|building_level_address)", re.I)
ODBL_VALUE = re.compile(r"osm|openstreetmap|odbl", re.I)
GEO_TYPES = {
    "POINT": "Point",
    "LINESTRING": "LineString",
    "POLYGON": "Polygon",
    "MULTIPOINT": "MultiPoint",
    "MULTILINESTRING": "MultiLineString",
    "MULTIPOLYGON": "MultiPolygon",
    "GEOMETRYCOLLECTION": "GeometryCollection",
}
COVERING = {"bbox": {k: ["bbox", k] for k in ("xmin", "ymin", "xmax", "ymax")}}


class PublishError(Exception):
    pass


def validate_parquet(path: Path, allow: tuple[str, ...] = (), require_geo: bool = True, ogl: bool = False) -> list[str]:
    """Return a list of problems with one parquet file (empty list = OK).

    ogl=True marks an OGL-licensed table: it must carry no ODbL-derived (osm_*) columns."""
    problems = []
    try:
        schema = pq.read_schema(path)
    except Exception as e:  # noqa: BLE001
        return [f"{path}: unreadable parquet ({e})"]
    if ogl:
        problems += [
            f"{path}: ODbL-derived column '{n}' in an OGL table" for n in schema.names if ODBL_COLUMN.search(n)
        ]
    for name in schema.names:
        if FORBIDDEN.search(name) and "*" not in allow and name not in allow and name not in ALLOWED_NAMES:
            problems.append(f"{path}: forbidden column '{name}'")
    if not require_geo:
        return problems
    md = schema.metadata or {}
    if b"geo" not in md:
        problems.append(f"{path}: missing GeoParquet 'geo' metadata")
    else:
        try:
            json.loads(md[b"geo"])
        except ValueError:
            problems.append(f"{path}: 'geo' metadata is not valid JSON")
    return problems


# --------------------------------------------------------------------------------------------------------
# GeoParquet 1.1 validation
# --------------------------------------------------------------------------------------------------------

_ENCODINGS = {"WKB", "point", "linestring", "polygon", "multipoint", "multilinestring", "multipolygon"}
_BBOX_TOL = 1e-6


def _connect(threads: int | None = None, memory_limit: str = "4GB") -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")
    if threads:
        con.execute(f"SET threads={int(threads)}")
    con.execute(f"SET memory_limit='{memory_limit}'")
    return con


def _q(path: Path | str) -> str:
    return str(path).replace("'", "''")


def _geom_expr(con, path: Path, col: str = "geometry") -> str:
    """SQL giving a GEOMETRY for the WKB column. DuckDB >= 1.5/2.0 already maps geo-annotated Parquet columns
    to GEOMETRY on read; older versions or files without 'geo' metadata give BLOB."""
    # DESCRIBE reads the schema, so this also works for zero-row files (e.g. a LAD with no matched outlines).
    rows = con.execute(f"DESCRIBE SELECT \"{col}\" FROM read_parquet('{_q(path)}')").fetchall()
    typ = str(rows[0][1])
    return f'"{col}"' if typ.upper().startswith("GEOMETRY") else f'ST_GeomFromWKB("{col}")'


def _check_geo_metadata(schema, geo: dict) -> list[str]:
    """Static checks of the 'geo' JSON against the GeoParquet 1.1 spec essentials."""
    p: list[str] = []
    if not isinstance(geo.get("version"), str):
        p.append("geo.version missing or not a string")
    elif not geo["version"].startswith("1.1"):
        p.append(f"geo.version is {geo['version']!r}, expected 1.1.x (covering needs 1.1)")
    cols = geo.get("columns")
    if not isinstance(cols, dict) or not cols:
        return p + ["geo.columns missing or empty"]
    prim = geo.get("primary_column")
    if prim not in cols:
        p.append(f"geo.primary_column {prim!r} not in geo.columns")
    for name, c in cols.items():
        if name not in schema.names:
            p.append(f"geo column {name!r} not in the Parquet schema")
            continue
        if c.get("encoding") not in _ENCODINGS:
            p.append(f"{name}: invalid encoding {c.get('encoding')!r}")
        gt = c.get("geometry_types")
        valid = set(GEO_TYPES.values())
        if (
            not isinstance(gt, list)
            or len(set(gt)) != len(gt)
            or any(not isinstance(t, str) or t.removesuffix(" Z") not in valid for t in gt)
        ):
            p.append(f"{name}: invalid geometry_types {gt!r}")
        crs = c.get("crs", "absent")
        if crs == "absent":
            p.append(f"{name}: crs absent (would default to OGC:CRS84, wrong for BNG)")
        elif isinstance(crs, dict):
            if "type" not in crs or "$schema" not in crs:
                p.append(f"{name}: crs is not PROJJSON ($schema/type missing)")
            if (crs.get("id") or {}).get("code") != 27700:
                p.append(f"{name}: crs id is not EPSG:27700")
        bb = c.get("bbox")
        if bb is not None and (
            len(bb) != 4 or not all(isinstance(v, (int, float)) for v in bb) or bb[0] > bb[2] or bb[1] > bb[3]
        ):
            p.append(f"{name}: invalid bbox {bb!r}")
        cov = c.get("covering")
        if cov is not None:
            b = cov.get("bbox") if isinstance(cov, dict) else None
            if not isinstance(b, dict) or set(b) != {"xmin", "ymin", "xmax", "ymax"}:
                p.append(f"{name}: covering.bbox must map xmin, ymin, xmax, ymax")
                continue
            roots = {tuple(v)[0] for v in b.values() if len(v) == 2}
            if len(roots) != 1 or any(len(v) != 2 or v[1] != k for k, v in b.items()):
                p.append(f"{name}: covering.bbox paths must be [<column>, <field>] with one shared column")
                continue
            root = roots.pop()
            if root not in schema.names:
                p.append(f"{name}: covering column {root!r} not in schema")
                continue
            t = schema.field(root).type
            fields = {t.field(i).name: t.field(i).type for i in range(t.num_fields)} if hasattr(t, "num_fields") else {}
            if (
                not all(k in fields for k in b)
                or len({str(fields[k]) for k in b}) != 1
                or str(fields["xmin"]) not in ("double", "float")
            ):
                p.append(f"{name}: covering column {root!r} must be a struct of 4 float/double fields")
    return p


def validate_geoparquet(path: Path, deep: bool = True, con=None, require_covering: bool = True) -> list[str]:
    """Check one file against GeoParquet 1.1 essentials; return problems (empty = OK).

    Static: 'geo' JSON schema essentials (version, primary_column, encoding, geometry_types, PROJJSON crs
    = EPSG:27700, bbox, covering struct). Row-group statistics exist for the covering fields.
    deep=True also decodes every geometry with DuckDB and checks: declared geometry_types equal the actual
    set, file bbox equals the data extent, covering == geometry bounds for every row (NULL iff NULL geometry).
    """
    path = Path(path)
    try:
        pf = pq.ParquetFile(path)
    except Exception as e:  # noqa: BLE001
        return [f"{path}: unreadable parquet ({e})"]
    schema = pf.schema_arrow
    raw = (schema.metadata or {}).get(b"geo")
    if raw is None:
        return [f"{path}: missing GeoParquet 'geo' metadata"]
    try:
        geo = json.loads(raw)
    except ValueError:
        return [f"{path}: 'geo' metadata is not valid JSON"]
    probs = [f"{path}: {m}" for m in _check_geo_metadata(schema, geo)]
    if probs:
        return probs
    col = geo["columns"][geo["primary_column"]]
    prim = geo["primary_column"]
    cov = col.get("covering")
    if cov is None:
        if require_covering:
            probs.append(f"{path}: no covering declared")
        if not deep:
            return probs
    else:
        root = cov["bbox"]["xmin"][0]
        md = pf.metadata
        for rg in range(md.num_row_groups):
            for ci in range(md.num_columns):
                cm = md.row_group(rg).column(ci)
                if cm.path_in_schema.startswith(root + ".") and not (cm.statistics and cm.statistics.has_min_max):
                    probs.append(f"{path}: row group {rg} has no min/max statistics for {cm.path_in_schema}")
                    break
    if deep and pf.metadata.num_rows:
        own = con is None
        con = con or _connect()
        try:
            bb = f', "{cov["bbox"]["xmin"][0]}" AS cb' if cov else ""
            cov_sql = ""
            if cov:
                t = _BBOX_TOL
                cov_sql = (
                    f", count(*) FILTER (WHERE (cb IS NULL) <> (g IS NULL) "
                    f"OR abs(cb.xmin - ST_XMin(g)) > {t} OR abs(cb.ymin - ST_YMin(g)) > {t} "
                    f"OR abs(cb.xmax - ST_XMax(g)) > {t} OR abs(cb.ymax - ST_YMax(g)) > {t})"
                )
            r = con.execute(
                f"""SELECT count(*), count(*) FILTER (WHERE g IS NULL),
                       list(DISTINCT ST_GeometryType(g)) FILTER (WHERE g IS NOT NULL),
                       min(ST_XMin(g)), min(ST_YMin(g)), max(ST_XMax(g)), max(ST_YMax(g)){cov_sql}
                    FROM (SELECT {_geom_expr(con, path, prim)} g{bb} FROM read_parquet('{_q(path)}'))"""
            ).fetchone()
        finally:
            if own:
                con.close()
        n, nnull, types, x0, y0, x1, y1 = r[:7]
        actual = sorted(GEO_TYPES.get(t, t) for t in (types or []))
        declared = sorted(t.removesuffix(" Z") for t in col["geometry_types"])
        if declared != actual:
            probs.append(f"{path}: geometry_types {declared} != actual {actual}")
        if col.get("bbox") and n > nnull:
            got = [x0, y0, x1, y1]
            if any(abs(a - b) > 1e-3 for a, b in zip(col["bbox"], got, strict=True)):
                probs.append(f"{path}: bbox {col['bbox']} != data extent {got}")
        if cov and r[7]:
            probs.append(f"{path}: covering bbox disagrees with geometry bounds in {r[7]} rows")
    return probs


def file_report(path: Path) -> dict:
    """Size and row-group statistics for one Parquet file (and bbox overlap of row groups if geo)."""
    pf = pq.ParquetFile(path)
    md = pf.metadata
    rows = [md.row_group(i).num_rows for i in range(md.num_row_groups)]
    rep = {
        "file": str(path),
        "bytes": Path(path).stat().st_size,
        "rows": md.num_rows,
        "row_groups": len(rows),
        "rg_rows_min": min(rows, default=0),
        "rg_rows_max": max(rows, default=0),
        "codecs": sorted({md.row_group(0).column(i).compression for i in range(md.num_columns)}) if rows else [],
    }
    names = [md.row_group(0).column(i).path_in_schema for i in range(md.num_columns)] if rows else []
    if "bbox.xmin" in names:
        i = {n: names.index(n) for n in ("bbox.xmin", "bbox.ymin", "bbox.xmax", "bbox.ymax")}
        area = 0.0
        ext = [float("inf"), float("inf"), float("-inf"), float("-inf")]
        for g in range(md.num_row_groups):
            s = {k: md.row_group(g).column(v).statistics for k, v in i.items()}
            w = s["bbox.xmax"].max - s["bbox.xmin"].min
            h = s["bbox.ymax"].max - s["bbox.ymin"].min
            area += w * h
            ext = [
                min(ext[0], s["bbox.xmin"].min),
                min(ext[1], s["bbox.ymin"].min),
                max(ext[2], s["bbox.xmax"].max),
                max(ext[3], s["bbox.ymax"].max),
            ]
        total = (ext[2] - ext[0]) * (ext[3] - ext[1])
        rep["rg_bbox_area_over_extent"] = round(area / total, 3) if total else None
    return rep


def bbox_query_report(path: Path, bbox: tuple[float, float, float, float], con=None) -> dict:
    """Time a bbox count with and without the covering column, plus rows the scan actually read.

    'with covering' filters on bbox.xmin/ymin/xmax/ymax (DuckDB turns these into row-group min/max
    pruning); 'without' decodes WKB and tests ST_Intersects. Rows scanned come from EXPLAIN ANALYZE.
    """
    import re
    import time

    own = con is None
    con = con or _connect()
    x0, y0, x1, y1 = bbox
    gx = _geom_expr(con, path)
    q_cov = (
        f"SELECT count(*) FROM read_parquet('{_q(path)}') WHERE bbox.xmin <= {x1} AND bbox.xmax >= {x0} "
        f"AND bbox.ymin <= {y1} AND bbox.ymax >= {y0}"
    )
    q_geo = (
        f"SELECT count(*) FROM read_parquet('{_q(path)}') WHERE ST_Intersects({gx}, "
        f"ST_MakeEnvelope({x0}, {y0}, {x1}, {y1}))"
    )
    out: dict = {}
    try:
        for label, q in (("covering", q_cov), ("wkb_scan", q_geo)):
            t = time.perf_counter()
            n = con.execute(q).fetchone()[0]
            dt = time.perf_counter() - t
            plan = "\n".join(str(r[1]) for r in con.execute("EXPLAIN ANALYZE " + q).fetchall())
            out[label] = {
                "count": n,
                "seconds": round(dt, 4),
                "plan": plan,
                "row_groups_scanned": re.findall(r"Row Groups Scanned: (\d+ / \d+)", plan),
            }
        out["explain_covering"] = con.execute("EXPLAIN " + q_cov).fetchall()[0][1]
    finally:
        if own:
            con.close()
    return out


# --------------------------------------------------------------------------------------------------------
# Release writer
# --------------------------------------------------------------------------------------------------------


def _lad_dirs(out_dir: Path, lads: list[str] | None = None) -> list[Path]:
    found = sorted(p for p in Path(out_dir).iterdir() if p.is_dir() and (p / "uprn.parquet").exists())
    if lads:
        found = [p for p in found if p.name.removeprefix("lad=") in set(lads)]
    return found


def _write_geo(con, src: Path, dst: Path, row_group_size: int, key: str = "uprn") -> dict:
    """Rewrite one geo file: bbox covering column, Hilbert order, 1.1.0 metadata. Returns file bbox/types."""
    from uprn_resolver.build import EPSG_27700_PROJJSON

    # the bbox covering column is recomputed (a source 'bbox' would otherwise be duplicated as 'bbox_1'); hive
    # partitioning is off so the path's lad=<code> is not materialised as a column of the release file
    srcq = f"read_parquet('{_q(src)}', hive_partitioning=false)"
    exclude = "g, bbox" if _has_file_col(con, src, "bbox") else "g"
    con.execute(f"""CREATE OR REPLACE TEMP TABLE t AS
        SELECT * EXCLUDE ({exclude}) REPLACE (ST_AsWKB(g)::BLOB AS geometry),
               CASE WHEN g IS NOT NULL THEN
                   {{'xmin': ST_XMin(g), 'ymin': ST_YMin(g), 'xmax': ST_XMax(g), 'ymax': ST_YMax(g)}}::
                   STRUCT(xmin DOUBLE, ymin DOUBLE, xmax DOUBLE, ymax DOUBLE) END AS bbox,
               ST_GeometryType(g)::VARCHAR AS _gt
        FROM (SELECT *, {_geom_expr(con, src)} AS g FROM {srcq})""")
    x0, y0, x1, y1, n, types = con.execute(
        "SELECT min(bbox.xmin), min(bbox.ymin), max(bbox.xmax), max(bbox.ymax), count(*), "
        "list(DISTINCT _gt) FILTER (WHERE _gt IS NOT NULL) FROM t"
    ).fetchone()
    col: dict = {
        "encoding": "WKB",
        "geometry_types": sorted(GEO_TYPES.get(t, t) for t in (types or [])),
        "crs": EPSG_27700_PROJJSON,
        "covering": COVERING,
    }
    if x0 is not None:
        col["bbox"] = [x0, y0, x1, y1]
    geo = json.dumps({"version": "1.1.0", "primary_column": "geometry", "columns": {"geometry": col}})
    e = BNG_EXTENT
    order = (
        f"ST_Hilbert((bbox.xmin + bbox.xmax) / 2, (bbox.ymin + bbox.ymax) / 2, "
        f"{{min_x: {e[0]}, min_y: {e[1]}, max_x: {e[2]}, max_y: {e[3]}}}::BOX_2D)"
    )
    key = key if _has(con, "t", key) else "building_id"
    _copy(con, f"SELECT * EXCLUDE (_gt) FROM t ORDER BY {order}, {key}", dst, row_group_size, geo)
    con.execute("DROP TABLE t")
    return {"rows": n, "bbox": col.get("bbox")}


def _has(con, table: str, column: str) -> bool:
    return column in [r[0] for r in con.execute(f"DESCRIBE {table}").fetchall()]


def _copy(con, query: str, dst: Path, row_group_size: int, geo: str | None = None) -> None:
    tmp = dst.with_name(dst.name + ".tmp")
    dst.parent.mkdir(parents=True, exist_ok=True)
    kv = f", KV_METADATA {{geo: '{geo.replace(chr(39), chr(39) * 2)}'}}" if geo else ""
    con.execute(
        f"COPY ({query}) TO '{_q(tmp)}' (FORMAT PARQUET, COMPRESSION ZSTD, "
        f"COMPRESSION_LEVEL {COMPRESSION_LEVEL}, ROW_GROUP_SIZE {row_group_size}{kv})"
    )
    tmp.replace(dst)


def _allow_for(kind: str, allow: tuple[str, ...]) -> tuple[str, ...]:
    """Allowing 'postcode' also allows the provenance postcode-text columns."""
    return (*allow, *PROVENANCE_POSTCODE_COLS) if kind == "provenance" and "postcode" in allow else allow


def _write_plain(con, src: Path, dst: Path, row_group_size: int, allow: tuple[str, ...]) -> int:
    srcq = f"read_parquet('{_q(src)}', hive_partitioning=false)"  # no materialised lad=<code> column
    cols = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {srcq}").fetchall()]
    if "postcode" not in allow:
        cols = [c for c in cols if c not in PROVENANCE_POSTCODE_COLS or dst.parent.parent.name != "provenance"]
    sel = ", ".join(f'"{c}"' for c in cols)
    order = " ORDER BY uprn" if "uprn" in cols else ""
    _copy(con, f"SELECT {sel} FROM {srcq}{order}", dst, row_group_size)
    return int(con.execute(f"SELECT count(*) FROM read_parquet('{_q(dst)}')").fetchone()[0])


def _table_allow(kind: str, allow: tuple[str, ...]) -> tuple[str, ...]:
    return (*_allow_for(kind, allow), *TABLE_ALLOW.get(kind, ()))


def _check_ogl_values(con, src: Path) -> list[str]:
    """resolved_open.best_outline_source must never name an OSM/ODbL source."""
    if not _has_file_col(con, src, "best_outline_source"):
        return []
    bad = [
        r[0]
        for r in con.execute(
            f"SELECT DISTINCT best_outline_source FROM read_parquet('{_q(src)}') WHERE best_outline_source IS NOT NULL"
        ).fetchall()
        if ODBL_VALUE.search(r[0])
    ]
    return [f"{src}: ODbL-derived best_outline_source value(s) {bad} in an OGL table"] if bad else []


def _has_file_col(con, src: Path, col: str) -> bool:
    return col in [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet('{_q(src)}')").fetchall()]


def _release_root(stage_dir: Path, release: str, out_dir: Path) -> Path:
    """``stage_dir/release``, refusing anything that is not a plain sub-folder of ``stage_dir`` (it is deleted and
    rebuilt by ``assemble``) or that overlaps the LAD outputs."""
    if not release or release in (".", "..") or "/" in release or "\\" in release or release.startswith("."):
        raise PublishError(f"unsafe release name {release!r}: must be a plain folder name")
    base = Path(stage_dir).resolve()
    root = (base / release).resolve()
    if root.parent != base:
        raise PublishError(f"release dir {root} is not directly under the stage dir {base}")
    out = Path(out_dir).resolve()
    if root == out or root in out.parents or out in root.parents:
        raise PublishError(f"release dir {root} overlaps the LAD output dir {out}")
    return Path(stage_dir) / release


def check_release_tree(root: Path) -> list[str]:
    """Content-based pre-upload check of everything under ``root`` (what ``aws s3 sync`` would send): no ``*.tmp``,
    no parquet outside the known table folders, and no parquet column that only exists in the local-only tables
    (address text, OSM address candidates, ``*_local`` outline columns), whatever the file is called."""
    root = Path(root)
    problems: list[str] = []
    known = {*GEO_TABLES, *OPTIONAL_GEO, *PLAIN_TABLES, ODBL_TABLE, "national"}
    for f in sorted(root.rglob("*")):
        if not f.is_file():
            continue
        rel = f.relative_to(root)
        if f.name.endswith(".tmp"):
            problems.append(f"{f}: leftover temporary file")
        if any(n in part for part in rel.parts for n in NEVER_SHIP):
            problems.append(f"{f}: local-only table must never be released")
        if f.suffix != ".parquet":
            continue
        if rel.parts != ("lad_index.parquet",) and rel.parts[0] not in known:
            problems.append(f"{f}: parquet outside the known release tables")
        try:
            names = pq.read_schema(f).names
        except Exception as e:  # noqa: BLE001
            problems.append(f"{f}: unreadable parquet ({e})")
            continue
        problems += [
            f"{f}: local-only column '{n}' must never be released" for n in names if NEVER_SHIP_COLUMNS.match(n)
        ]
    return problems


def assemble(out_dir: Path, release: str, stage_dir: Path, *args, force: bool = False, **kwargs) -> list[Path]:
    """Write and validate a release (see ``_assemble``). A failed run removes its partial release folder, so the
    immutability check never blocks a retry."""
    root = _release_root(stage_dir, release, out_dir)
    if root.exists() and not force:
        raise PublishError(
            f"release {release!r} already exists at {root}: releases are immutable (use force to rebuild)"
        )
    try:
        return _assemble(out_dir, release, stage_dir, *args, force=force, **kwargs)
    except BaseException:
        shutil.rmtree(root, ignore_errors=True)
        raise


def _assemble(
    out_dir: Path,
    release: str,
    stage_dir: Path,
    allow: tuple[str, ...] = (),
    *,
    lads: list[str] | None = None,
    row_group_size: int = ROW_GROUP_SIZE,
    threads: int | None = None,
    memory_limit: str = "4GB",
    deep_validate: bool = True,
    force: bool = False,
    stac: bool = True,
    national_files: bool = False,
    changelog_against: Path | None = None,
    source_manifest: Path | None = None,
) -> list[Path]:
    """Write <stage_dir>/<release>/ from <out_dir>/lad=<code>/*.parquet and validate every file.

    Layout (hive partitioned by lad, one file per LAD per table). GeoParquet 1.1 (Hilbert, bbox covering):
    uprn/, buildings/, resolved_open/ (OGL; poi_* columns Overture, outline ids Microsoft), outline_ms_buildings/
    (CDLA-Permissive-2.0), outline_odbl_buildings/ (ODbL-1.0). Plain Parquet: epc/, provenance/ (OGL), poi/
    (Overture), outline_ms/ (CDLA-Permissive-2.0), streets_odbl/, outline_odbl/ (ODbL-1.0). Every non-OGL-core
    folder (and resolved_open) gets a LICENCE.txt copied from the LAD sidecar, and is required to have one.
    ODbL tables are separate folders and are never joined into the OGL tables. Also lad_index.parquet,
    stats.json, README.md, LICENCE.md. address_local and resolved_local are never read.
    Releases are immutable: an existing ``<stage_dir>/<release>`` is refused unless ``force=True``, which deletes
    and rebuilds it from scratch (it must be a plain sub-folder of ``stage_dir``). The final tree is checked by
    content (``check_release_tree``). Extras (all inside the release folder): ``stac/`` static STAC catalogue
    (``stac=True``), ``national/<table>/part-NNNN.parquet`` Hilbert-sorted national files (``national_files=True``,
    off by default: needs scratch disk), ``CHANGELOG.json/.md`` against ``changelog_against`` (a previous release
    dir), and last ``release_manifest.json`` (sizes, sha256, row counts, source versions). ``<stage_dir>/latest.json``
    is updated to point at this release. Returns the files written.
    """
    lad_dirs = _lad_dirs(out_dir, lads)
    if not lad_dirs:
        raise PublishError(f"no <lad>/uprn.parquet found under {out_dir}")
    root = _release_root(stage_dir, release, out_dir)
    if root.exists() and not force:
        raise PublishError(
            f"release {release!r} already exists at {root}: releases are immutable (use force to rebuild)"
        )
    problems: list[str] = []
    jobs: list[tuple[str, Path, Path]] = []  # (kind, src, dst)
    optional = (*OPTIONAL_GEO, *PLAIN_TABLES, ODBL_TABLE)
    assert not set(NEVER_SHIP) & {*GEO_TABLES, *optional}
    for d in lad_dirs:
        code = d.name.removeprefix("lad=")
        for kind in GEO_TABLES:
            src = d / f"{kind}.parquet"
            if not src.exists():
                problems.append(f"{src}: missing")
                continue
            problems += validate_parquet(src, allow, ogl=True)
            jobs.append((kind, src, root / kind / f"lad={code}" / f"{kind}.parquet"))
        for kind in optional:
            src = d / f"{kind}.parquet"
            if not src.exists():
                continue
            if kind in OPTIONAL_GEO:
                problems += validate_parquet(src, _table_allow(kind, allow), ogl=kind in OGL_TABLES)
            elif kind == "provenance":
                # provenance postcode columns are dropped on write unless allowed, so check what is written
                a = _allow_for(kind, allow) if "postcode" in allow else (*allow, *PROVENANCE_POSTCODE_COLS)
                problems += validate_parquet(src, a, require_geo=False, ogl=True)
            else:
                problems += validate_parquet(src, _table_allow(kind, allow), require_geo=False, ogl=kind in OGL_TABLES)
            jobs.append((kind, src, root / kind / f"lad={code}" / f"{kind}.parquet"))
            sidecar = LICENCE_SIDECAR.get(kind)
            if sidecar and not (d / sidecar).exists():
                problems.append(f"{d}: {kind}.parquet has no {sidecar}")
    if problems:
        raise PublishError("validation failed:\n  " + "\n  ".join(problems))

    # start from a clean release folder: a previous assemble's files (excluded LADs, tables whose source was removed,
    # crashed *.tmp) would otherwise be uploaded by `aws s3 sync`
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)
    files: list[Path] = []
    index: list[dict] = []
    con = _connect(threads, memory_limit)
    con.execute(f"SET temp_directory='{_q(Path(stage_dir).resolve() / '.duckdb_tmp')}'")
    try:
        for kind, src, _ in jobs:
            if kind in OGL_TABLES and kind not in GEO_TABLES:
                problems += _check_ogl_values(con, src)
        if problems:
            raise PublishError("validation failed:\n  " + "\n  ".join(problems))
        for kind, src, dst in jobs:
            if kind in GEO_TABLES or kind in OPTIONAL_GEO:
                info = _write_geo(con, src, dst, row_group_size, OPTIONAL_GEO.get(kind, "uprn"))
                if kind == "uprn":
                    b = info["bbox"] or [None] * 4
                    index.append(
                        {
                            "lad": dst.parent.name.removeprefix("lad="),
                            "n_uprn": info["rows"],
                            "xmin": b[0],
                            "ymin": b[1],
                            "xmax": b[2],
                            "ymax": b[3],
                            "bytes": 0,
                        }
                    )
            else:
                _write_plain(con, src, dst, row_group_size, allow)
            files.append(dst)
        out_problems: list[str] = []
        for kind, _, dst in jobs:
            if kind in GEO_TABLES or kind in OPTIONAL_GEO:
                out_problems += validate_geoparquet(dst, deep=deep_validate, con=con)
            out_problems += validate_parquet(dst, _table_allow(kind, allow), require_geo=False, ogl=kind in OGL_TABLES)
    finally:
        con.close()
    if out_problems:
        raise PublishError("release validation failed:\n  " + "\n  ".join(out_problems))
    for row in index:
        row["bytes"] = (root / "uprn" / f"lad={row['lad']}" / "uprn.parquet").stat().st_size
    import pyarrow as pa

    pq.write_table(pa.Table.from_pylist(index), root / "lad_index.parquet", compression="zstd")
    files.append(root / "lad_index.parquet")
    for kind, sidecar in LICENCE_SIDECAR.items():
        for d in lad_dirs:
            if (d / sidecar).exists() and (root / kind).exists():
                shutil.copy2(d / sidecar, root / kind / "LICENCE.txt")
                files.append(root / kind / "LICENCE.txt")
                break
    stats = {}
    for d in lad_dirs:
        f = d / "stats.json"
        if f.exists():
            stats[d.name.removeprefix("lad=")] = json.loads(f.read_text())
    (root / "stats.json").write_text(json.dumps(stats, indent=2))
    files.append(root / "stats.json")
    for name in ("README.md", "LICENCE.md"):
        shutil.copy2(PUBLISH_DIR / name, root / name)
        files.append(root / name)
    assert not any(n in f.name or n in f.parts for f in files for n in NEVER_SHIP)
    assert not [f for f in root.rglob("*") if any(n in f.name or n in f.parts for n in NEVER_SHIP)]
    if national_files:
        files += write_national(root, memory_limit=memory_limit, threads=threads)
    if changelog_against:
        diff_releases(changelog_against, root, write=True)
        files += [root / "CHANGELOG.json", root / "CHANGELOG.md"]
    if stac:
        from .stac import build_stac

        res = build_stac(root, release)
        if res.get("invalid") or res.get("missing_assets"):
            raise PublishError(f"STAC validation failed: {res.get('invalid') or res['missing_assets'][:5]}")
        files += [p for p in (root / "stac").rglob("*") if p.is_file()]
    tree_problems = check_release_tree(root)
    if tree_problems:
        raise PublishError("release tree check failed:\n  " + "\n  ".join(tree_problems))
    write_manifest(root, release, source_manifest=source_manifest)
    files.append(root / "release_manifest.json")
    write_latest(Path(stage_dir), release)
    return files


# --------------------------------------------------------------------------------------------------------
# Release manifest, latest pointer, national files, changelog
# --------------------------------------------------------------------------------------------------------

NATIONAL_PART_BYTES = 2 * 1024**3  # GeoParquet distribution guidance: partition spatially above ~2 GB
# id column that identifies a feature across LAD files (buildings straddling two LADs appear in both)
NATIONAL_KEY = {
    "uprn": "uprn",
    "resolved_open": "uprn",
    "buildings": "building_id",
    "outline_ms_buildings": "ms_building_id",
    "outline_odbl_buildings": "osm_building_id",
}
# columns whose value changes are counted per table by diff_releases
TRACKED_COLUMNS = {
    "uprn": ("building_id", "street_name", "toid", "parcel_inspire_id"),
    "resolved_open": ("best_outline_id", "outline_confidence", "street_name"),
}
# tables diffed at UPRN level (they carry a ``uprn`` column)
DIFF_TABLES = ("uprn", "resolved_open", "epc", "provenance", "poi", "outline_ms", "outline_odbl", "streets_odbl")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(8 << 20):
            h.update(chunk)
    return h.hexdigest()


def summarise_source_manifest(path: Path | None) -> dict:
    """Digest of ``data/parquet/manifest.json``: its sha256, downloaded files and converted rows per dataset."""
    if path is None or not Path(path).exists():
        return {}
    path = Path(path)
    m = json.loads(path.read_text())
    ds: dict[str, dict] = {}
    for name, e in (m.get("downloads") or {}).items():
        d = ds.setdefault(e.get("dataset", "?"), {"downloads": [], "converted_files": 0, "converted_rows": 0})
        d["downloads"].append({"file": name, "recorded_at": e.get("recorded_at"), "rows": e.get("rows")})
    for e in (m.get("converted") or {}).values():
        d = ds.setdefault(e.get("dataset", "?"), {"downloads": [], "converted_files": 0, "converted_rows": 0})
        d["converted_files"] += 1
        d["converted_rows"] += int(e.get("rows") or 0)
    return {"manifest": path.name, "sha256": _sha256(path), "datasets": dict(sorted(ds.items()))}


def write_manifest(
    root: Path, release: str | None = None, source_manifest: Path | None = None, workers: int = 4
) -> dict:
    """Write ``<root>/release_manifest.json``: every file with size and sha256 (parquet also row count and row
    groups), plus source manifest versions. The manifest does not list itself. Returns the manifest dict."""
    root = Path(root)
    paths = sorted(p for p in root.rglob("*") if p.is_file() and p.name != "release_manifest.json")

    def one(p: Path) -> dict:
        e = {"path": p.relative_to(root).as_posix(), "bytes": p.stat().st_size, "sha256": _sha256(p)}
        if p.suffix == ".parquet":
            md = pq.ParquetFile(p).metadata
            e["rows"], e["row_groups"] = md.num_rows, md.num_row_groups
        return e

    with ThreadPoolExecutor(workers) as ex:
        entries = list(ex.map(one, paths))
    tables: dict[str, dict] = {}
    for e in entries:
        if e["path"].endswith(".parquet") and "/" in e["path"]:
            t = tables.setdefault(e["path"].split("/")[0], {"files": 0, "rows": 0, "bytes": 0})
            t["files"] += 1
            t["rows"] += e["rows"]
            t["bytes"] += e["bytes"]
    manifest = {
        "release": release or root.name,
        "created": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "n_files": len(entries),
        "total_bytes": sum(e["bytes"] for e in entries),
        "tables": tables,
        "source": summarise_source_manifest(source_manifest),
        "files": entries,
    }
    (root / "release_manifest.json").write_text(json.dumps(manifest, indent=1))
    return manifest


def write_latest(stage_dir: Path, release: str) -> dict:
    """``<stage_dir>/latest.json``: pointer to the newest release (upload it to the product root)."""
    latest = {
        "latest": release,
        "path": f"{release}/",
        "manifest": f"{release}/release_manifest.json",
        "stac": f"{release}/stac/catalog.json",
        "updated": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
    }
    (Path(stage_dir) / "latest.json").write_text(json.dumps(latest, indent=1))
    return latest


def write_national(
    root: Path,
    tables: tuple[str, ...] | None = None,
    *,
    memory_limit: str = "4GB",
    threads: int | None = None,
    part_bytes: int = NATIONAL_PART_BYTES,
    row_group_size: int = ROW_GROUP_SIZE,
) -> list[Path]:
    """Also write each geo table as national Hilbert-sorted GeoParquet(s) with bbox covering, for remote readers
    that want few requests: ``national/<table>/part-NNNN.parquet``. Split into contiguous Hilbert ranges (spatial
    partitions) when the LAD files exceed ``part_bytes`` (~2 GB). Features present in several LAD files (buildings
    straddling LADs) are deduplicated on their id. Reads the already-written LAD files in ``root``; needs scratch
    disk about the size of the table (DuckDB spills to ``<root>/../.duck_tmp``)."""
    root = Path(root)
    out: list[Path] = []
    tmp = root.parent / ".duck_tmp"
    con = _connect(threads, memory_limit)
    con.execute(f"SET temp_directory='{_q(tmp)}'")
    e = BNG_EXTENT
    try:
        for kind in tables or (*GEO_TABLES, *OPTIONAL_GEO):
            lads = sorted((root / kind).glob("lad=*/*.parquet"))
            if not lads:
                continue
            first = pq.read_schema(lads[0])
            geo = json.loads(first.metadata[b"geo"])
            srcq = f"read_parquet('{_q(root / kind)}/lad=*/{kind}.parquet', hive_partitioning=false)"
            key = NATIONAL_KEY[kind] if NATIONAL_KEY[kind] in first.names else "building_id"
            h = (
                f"ST_Hilbert((bbox.xmin + bbox.xmax) / 2, (bbox.ymin + bbox.ymax) / 2, "
                f"{{min_x: {e[0]}, min_y: {e[1]}, max_x: {e[2]}, max_y: {e[3]}}}::BOX_2D)"
            )
            con.execute(
                f"CREATE OR REPLACE TEMP TABLE nat AS SELECT *, {h} AS _h FROM {srcq} "
                + ("" if key == "uprn" else f'QUALIFY row_number() OVER (PARTITION BY "{key}") = 1')
            )
            total = sum(f.stat().st_size for f in lads)
            parts = max(1, -(-total // part_bytes))
            col = geo["columns"]["geometry"]
            bounds = (
                [
                    r[0]
                    for r in con.execute(
                        f"SELECT min(_h) FROM (SELECT _h, ntile({parts}) OVER (ORDER BY _h) AS g FROM nat) "
                        "GROUP BY g ORDER BY g"
                    ).fetchall()
                ]
                if parts > 1
                else [None]
            )
            for i, lo in enumerate(bounds):
                hi = bounds[i + 1] if i + 1 < len(bounds) else None
                cond = (
                    " AND ".join(
                        c
                        for c in (f"_h >= {lo}" if lo is not None and i else "", f"_h < {hi}" if hi is not None else "")
                        if c
                    )
                    or "true"
                )
                dst = root / "national" / kind / f"part-{i:04d}.parquet"
                x0, y0, x1, y1 = con.execute(
                    f"SELECT min(bbox.xmin), min(bbox.ymin), max(bbox.xmax), max(bbox.ymax) FROM nat WHERE {cond}"
                ).fetchone()
                col.pop("bbox", None)
                if x0 is not None:
                    col["bbox"] = [x0, y0, x1, y1]
                _copy(
                    con,
                    f'SELECT * EXCLUDE (_h) FROM nat WHERE {cond} ORDER BY _h, "{key}"',
                    dst,
                    row_group_size,
                    json.dumps(geo),
                )
                out.append(dst)
            con.execute("DROP TABLE nat")
    finally:
        con.close()
        shutil.rmtree(tmp, ignore_errors=True)
    return out


def _glob(root: Path, kind: str) -> str | None:
    return f"{_q(root / kind)}/lad=*/*.parquet" if any((root / kind).glob("lad=*/*.parquet")) else None


def diff_releases(
    old_dir: Path,
    new_dir: Path,
    *,
    write: bool = False,
    memory_limit: str = "4GB",
    threads: int | None = None,
    sample: int = 20,
) -> dict:
    """UPRN-level changes between two release folders, per table that has a ``uprn`` column: UPRNs added / removed,
    plus (``uprn`` and ``resolved_open``) the number of common UPRNs whose tracked columns changed
    (``TRACKED_COLUMNS``, e.g. best_outline_id, outline_confidence, street_name). Tables present in only one release
    are reported as ``added_table`` / ``removed_table``. With ``write=True`` writes ``CHANGELOG.json`` (counts and up to
    ``sample`` example UPRNs) and a short ``CHANGELOG.md`` into ``new_dir``."""
    old_dir, new_dir = Path(old_dir), Path(new_dir)
    con = _connect(threads, memory_limit)
    con.execute(f"SET temp_directory='{_q(new_dir.parent / '.duck_tmp')}'")
    tables: dict[str, dict] = {}
    try:
        for t in DIFF_TABLES:
            go, gn = _glob(old_dir, t), _glob(new_dir, t)
            if not go and not gn:
                continue
            if not go or not gn:
                tables[t] = {"status": "added_table" if gn else "removed_table"}
                continue
            ro = f"read_parquet('{go}', hive_partitioning=false)"
            rn = f"read_parquet('{gn}', hive_partitioning=false)"
            con.execute(f"CREATE OR REPLACE TEMP TABLE o AS SELECT DISTINCT uprn FROM {ro}")
            con.execute(f"CREATE OR REPLACE TEMP TABLE n AS SELECT DISTINCT uprn FROM {rn}")
            added = con.execute("SELECT count(*) FROM n WHERE uprn NOT IN (SELECT uprn FROM o)").fetchone()[0]
            removed = con.execute("SELECT count(*) FROM o WHERE uprn NOT IN (SELECT uprn FROM n)").fetchone()[0]
            entry: dict = {
                "old_uprns": con.execute("SELECT count(*) FROM o").fetchone()[0],
                "new_uprns": con.execute("SELECT count(*) FROM n").fetchone()[0],
                "added": added,
                "removed": removed,
                "sample_added": [
                    r[0]
                    for r in con.execute(
                        f"SELECT uprn FROM n WHERE uprn NOT IN (SELECT uprn FROM o) ORDER BY uprn LIMIT {sample}"
                    ).fetchall()
                ],
                "sample_removed": [
                    r[0]
                    for r in con.execute(
                        f"SELECT uprn FROM o WHERE uprn NOT IN (SELECT uprn FROM n) ORDER BY uprn LIMIT {sample}"
                    ).fetchall()
                ],
            }
            both = set(_cols(con, ro)) & set(_cols(con, rn))
            cols = [c for c in TRACKED_COLUMNS.get(t, ()) if c in both]
            if cols:
                sel = ", ".join(f'"{c}"' for c in cols)
                con.execute(f"CREATE OR REPLACE TEMP TABLE oo AS SELECT uprn, {sel} FROM {ro}")
                con.execute(f"CREATE OR REPLACE TEMP TABLE nn AS SELECT uprn, {sel} FROM {rn}")
                cnt = ", ".join(f'count(*) FILTER (WHERE oo."{c}" IS DISTINCT FROM nn."{c}")' for c in cols)
                row = con.execute(f"SELECT {cnt} FROM oo JOIN nn USING (uprn)").fetchone()
                entry["changed"] = dict(zip(cols, (int(v) for v in row), strict=True))
                con.execute("DROP TABLE oo; DROP TABLE nn")
            con.execute("DROP TABLE o; DROP TABLE n")
            tables[t] = entry
    finally:
        con.close()
        shutil.rmtree(new_dir.parent / ".duck_tmp", ignore_errors=True)
    result = {
        "old_release": old_dir.name,
        "new_release": new_dir.name,
        "generated": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "tables": tables,
    }
    if write:
        (new_dir / "CHANGELOG.json").write_text(json.dumps(result, indent=1))
        (new_dir / "CHANGELOG.md").write_text(_changelog_md(result))
    return result


def _cols(con, rel: str) -> list[str]:
    return [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {rel}").fetchall()]


def _changelog_md(r: dict) -> str:
    lines = [
        f"# Changes {r['old_release']} to {r['new_release']}",
        "",
        "UPRN-level changes per table (details and samples in CHANGELOG.json).",
        "",
        "| Table | UPRNs old | UPRNs new | Added | Removed | Changed values |",
        "|---|---|---|---|---|---|",
    ]
    for t, e in r["tables"].items():
        if "status" in e:
            lines.append(f"| {t} | | | | | {e['status'].replace('_', ' ')} |")
            continue
        ch = ", ".join(f"{k}: {v:,}" for k, v in e.get("changed", {}).items()) or "-"
        lines.append(f"| {t} | {e['old_uprns']:,} | {e['new_uprns']:,} | {e['added']:,} | {e['removed']:,} | {ch} |")
    return "\n".join(lines) + "\n"


def publish(
    out_dir: Path,
    release: str,
    dest: str,
    dry_run: bool = True,
    stage_dir: Path | None = None,
    allow: tuple[str, ...] = (),
    **assemble_kwargs,
) -> list:
    """Assemble + validate, then upload `<stage>/<release>/` to `dest` (s3://account/product/).

    Returns the list of planned/performed actions (command strings). Real upload only if
    dry_run=False.
    """
    out_dir = Path(out_dir)
    if not dest.startswith("s3://"):
        raise PublishError("dest must look like s3://<account>/<product>/")
    stage_dir = Path(stage_dir) if stage_dir else out_dir.parent / "release"
    files = assemble(out_dir, release, stage_dir, allow, **assemble_kwargs)
    target = dest.rstrip("/") + f"/{release}/"
    cmd = ["aws", "s3", "sync", str(stage_dir / release), target, "--endpoint-url", ENDPOINT]
    latest = [
        "aws",
        "s3",
        "cp",
        str(stage_dir / "latest.json"),
        dest.rstrip("/") + "/latest.json",
        "--endpoint-url",
        ENDPOINT,
    ]
    actions = [f"stage {len(files)} files in {stage_dir / release}", " ".join(cmd), " ".join(latest)]
    if dry_run:
        for a in actions:
            print(f"[dry-run] {a}")
        return actions
    if shutil.which("aws") is None:
        raise PublishError("aws CLI not found on PATH")
    subprocess.run(cmd, check=True)
    subprocess.run(latest, check=True)  # last, so readers never see a pointer to a half-uploaded release
    return actions


if __name__ == "__main__":  # python -m uprn_resolver.publish FILE...  : validate GeoParquet files, print reports
    import sys

    bad = 0
    for arg in sys.argv[1:]:
        probs = validate_geoparquet(Path(arg))
        bad += len(probs)
        print(json.dumps(file_report(Path(arg))), *(probs or ["valid GeoParquet 1.1"]), sep="\n  ")
    raise SystemExit(1 if bad else 0)
