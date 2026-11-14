"""Independent check of ``resolved_open`` outlines against OpenStreetMap buildings that mappers tagged with a UPRN.

About 3 million OSM building polygons in Great Britain carry ``ref:GB:uprn``. Each one is a building outline drawn by
a mapper and linked to a UPRN by a mapper, so it is evidence that does not come from this pipeline (no OSM data
reaches ``resolved_open``). For every tagged UPRN this module compares the mapped building with the UPRN's
``best_outline_id`` geometry and records:

* ``inside``: at least half of the mapped building lies inside our outline (the outline is in the right place);
* ``exclusive``: ``inside``, and no other tagged building (a different property, by its UPRN tag) has at least half
  its area inside our outline (the outline does not also cover a neighbour);
* ``area_ratio``: our outline area / mapped building area (1 for a matching building outline, above 1 for a parcel
  with a garden or a block);
* ``iou``: intersection over union of the two shapes.

Results are aggregated by ``best_outline_source`` x ``outline_confidence``, split into buildings tagged with one UPRN
(houses) and with several (flats, mixed use). The per-UPRN rows stay local (ODbL); only aggregate statistics are
written for the release, which are a Produced Work under the ODbL and carry the OSM attribution.

Caveat stated in the output: mappers often place the tag using the OS Open UPRN point, the same point this
pipeline uses, so ``inside`` mainly tests that our outline is the building or parcel around that point rather than an
independent survey of where the property is. ``exclusive`` and ``area_ratio`` test what the confidence label claims.

    uv run uprn-resolver accuracy --release 2026-10      # -> data/validation/accuracy_2026-10.json
"""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow.parquet as pq

from .convert import _connect, _load_spatial, _q
from .names import PUBLIC_SOURCES
from .publish import _geom_expr

ATTRIBUTION = "Contains OpenStreetMap data © OpenStreetMap contributors, ODbL-1.0 (aggregate statistics only)"
MIN_SHARE = 0.5  # share of a mapped building's area that must lie inside an outline to count as inside it


def _lad_file(base: Path, table: str, lad: str) -> Path:
    """One LAD's file in a per-LAD release (``<table>/lad=X/<table>.parquet``) or in the pipeline output
    (``lad=X/<table>.parquet``, e.g. ``data/out``)."""
    f = base / table / f"lad={lad}" / f"{table}.parquet"
    return f if f.exists() else base / f"lad={lad}" / f"{table}.parquet"


def _all_files(base: Path, table: str) -> list[Path]:
    return sorted((base / table).glob("lad=*/*.parquet")) or sorted(base.glob(f"lad=*/{table}.parquet"))


def _lads(base: Path) -> list[str]:
    return sorted({p.parent.name.removeprefix("lad=") for p in _all_files(base, "resolved_open")})


def prepare(release: Path, parquet_dir: Path, work: Path, memory_limit: str = "4GB") -> dict:
    """Once per run: tagged OSM buildings (one row per UPRN tag) and the parcel polygons used as best outlines."""
    work.mkdir(parents=True, exist_ok=True)
    con = _connect(parquet_dir, memory_limit)
    try:
        _load_spatial(con)
        con.execute("SET preserve_insertion_order=false")
        addr = parquet_dir / "osm" / "addresses.parquet"
        con.execute(
            f"""COPY (
                SELECT uprn, osm_type || '/' || osm_id AS osm_key, n_tag_uprns, geom_wkb,
                       ST_XMin(g) AS xmin, ST_YMin(g) AS ymin, ST_XMax(g) AS xmax, ST_YMax(g) AS ymax,
                       ST_Area(g) AS area
                FROM (SELECT try_cast(trim(unnest(string_split(ref_gb_uprn, ';'))) AS BIGINT) AS uprn, osm_type, osm_id,
                             len(string_split(ref_gb_uprn, ';')) AS n_tag_uprns, geom_wkb,
                             ST_GeomFromWKB(geom_wkb) AS g
                      FROM read_parquet({_q(addr)}) WHERE is_building AND ref_gb_uprn IS NOT NULL)
                WHERE uprn IS NOT NULL AND ST_Area(g) > 0
            ) TO {_q(work / "osm_tags.parquet")} (FORMAT parquet, COMPRESSION zstd)"""
        )
        files = _all_files(release, "resolved_open")
        hive = "lad" not in pq.read_schema(files[0]).names
        ro = f"read_parquet({[str(f) for f in files]}, hive_partitioning={str(hive).lower()})"
        parcels = work / "parcels"
        parcels.mkdir(exist_ok=True)
        con.execute("SET threads=2")
        for src, files in (("inspire_parcel", "inspire"), ("ros_parcel", "ros")):
            # ids first (small), then one pass over the national polygons keeping only those ids
            con.execute(
                f"""CREATE OR REPLACE TEMP TABLE ids AS SELECT DISTINCT lad, best_outline_id AS outline_id
                    FROM {ro} WHERE best_outline_source = {_q(src)}"""
            )
            con.execute(
                f"""COPY (
                    SELECT i.lad, i.outline_id, p.geom_wkb
                    FROM read_parquet({_q(parquet_dir / files / "*.parquet")}) p
                    JOIN ids i ON i.outline_id = p.inspire_id
                ) TO {_q(parcels / f"{src}.parquet")} (FORMAT parquet, COMPRESSION zstd)"""
            )
        n = con.execute(
            f"SELECT count(*), count(DISTINCT uprn) FROM read_parquet({_q(work / 'osm_tags.parquet')})"
        ).fetchone()
    finally:
        con.close()
    return {"osm_building_tags": n[0], "tagged_uprns": n[1]}


def lad_rows(lad: str, release: Path, parquet_dir: Path, work: Path, memory_limit: str = "4GB") -> int:
    """Per-UPRN comparison for one LAD, written to ``work/rows/lad=<lad>.parquet``; returns the row count."""
    out = work / "rows" / f"lad={lad}.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    con = _connect(parquet_dir, memory_limit)
    try:
        _load_spatial(con)

        rel = lambda t: _lad_file(release, t, lad)  # noqa: E731
        con.execute(
            f"""CREATE TEMP TABLE ro AS SELECT uprn, best_outline_source AS src, best_outline_id AS outline_id,
                       outline_confidence AS conf
                FROM read_parquet({_q(rel("resolved_open"))}, hive_partitioning=false)"""
        )
        con.execute(
            f"""CREATE TEMP TABLE tags AS SELECT t.*, ro.src, ro.outline_id, ro.conf
                FROM read_parquet({_q(work / "osm_tags.parquet")}) t JOIN ro USING (uprn)"""
        )
        if not con.execute("SELECT count(*) FROM tags").fetchone()[0]:
            con.execute(
                f"""COPY (SELECT NULL::BIGINT AS uprn, NULL::VARCHAR AS src, NULL::VARCHAR AS conf,
                             NULL::BIGINT AS n_tag_uprns, NULL::DOUBLE AS osm_area, NULL::DOUBLE AS outline_area,
                             NULL::DOUBLE AS inter, NULL::BOOLEAN AS inside, NULL::BOOLEAN AS no_neighbour WHERE false)
                    TO {_q(out)} (FORMAT parquet)"""
            )
            return 0
        x0, y0, x1, y1 = con.execute("SELECT min(xmin), min(ymin), max(xmax), max(ymax) FROM tags").fetchone()
        geoms = [
            ("openmap_building", rel("buildings"), "building_id"),
            ("ms_building", rel("outline_ms_buildings"), "ms_building_id"),
        ]
        parts = [
            f"""SELECT {_q(src)} AS src, {key} AS outline_id, {_geom_expr(con, path)} AS g
                FROM read_parquet({_q(path)}, hive_partitioning=false)
                WHERE {key} IN (SELECT outline_id FROM tags WHERE src = {_q(src)})"""
            for src, path, key in geoms
            if path.exists()
        ]
        parts += [
            f"""SELECT {_q(src)} AS src, outline_id, ST_GeomFromWKB(geom_wkb) AS g
                FROM read_parquet({_q(work / "parcels" / f"{src}.parquet")})
                WHERE lad = {_q(lad)} AND outline_id IN (SELECT outline_id FROM tags WHERE src = {_q(src)})"""
            for src in ("inspire_parcel", "ros_parcel")
        ]
        con.execute(
            f"CREATE TEMP TABLE outl AS SELECT DISTINCT ON (src, outline_id) * FROM ({' UNION ALL '.join(parts)})"
        )
        # every tagged building near the LAD, once per building with its UPRN set, for the neighbour test
        con.execute(
            f"""CREATE TEMP TABLE bld AS
                SELECT osm_key, list(uprn) AS uprns, any_value(ST_GeomFromWKB(geom_wkb)) AS g, any_value(area) AS area
                FROM read_parquet({_q(work / "osm_tags.parquet")})
                WHERE xmax >= {x0 - 500} AND xmin <= {x1 + 500} AND ymax >= {y0 - 500} AND ymin <= {y1 + 500}
                GROUP BY osm_key"""
        )
        con.execute(
            f"""CREATE TEMP TABLE inside AS
                SELECT o.src, o.outline_id, b.osm_key, b.uprns
                FROM outl o JOIN bld b ON ST_Intersects(o.g, b.g)
                WHERE ST_Area(ST_Intersection(o.g, b.g)) >= {MIN_SHARE} * b.area"""
        )
        con.execute(
            f"""COPY (
                SELECT t.uprn, t.src, t.conf, t.n_tag_uprns, t.area AS osm_area, ST_Area(o.g) AS outline_area,
                       coalesce(ST_Area(ST_Intersection(o.g, ST_GeomFromWKB(t.geom_wkb))), 0) AS inter,
                       EXISTS (SELECT 1 FROM inside i WHERE i.src = t.src AND i.outline_id = t.outline_id
                               AND i.osm_key = t.osm_key) AS inside,
                       NOT EXISTS (SELECT 1 FROM inside i WHERE i.src = t.src AND i.outline_id = t.outline_id
                                   AND NOT list_contains(i.uprns, t.uprn)) AS no_neighbour
                FROM tags t LEFT JOIN outl o USING (src, outline_id)
            ) TO {_q(out)} (FORMAT parquet, COMPRESSION zstd)"""
        )
        return con.execute(f"SELECT count(*) FROM read_parquet({_q(out)})").fetchone()[0]
    finally:
        con.close()


def summarise(work: Path, parquet_dir: Path) -> dict:
    """Aggregate the per-LAD rows: one entry per (source, confidence, single/multi-UPRN building)."""
    con = _connect(parquet_dir)
    try:
        rows = con.execute(
            f"""SELECT src, conf, CASE WHEN n_tag_uprns = 1 THEN 'single' ELSE 'multi' END AS building,
                       count(*) AS n,
                       round(100 * avg(inside::INT), 1) AS inside_pct,
                       round(100 * avg((inside AND no_neighbour)::INT), 1) AS exclusive_pct,
                       round(median(outline_area / osm_area) FILTER (WHERE outline_area > 0), 2) AS median_area_ratio,
                       round(median(inter / (osm_area + outline_area - inter)) FILTER (WHERE outline_area > 0), 2)
                           AS median_iou
                FROM read_parquet({_q(work / "rows" / "*.parquet")})
                GROUP BY GROUPING SETS ((src, conf, building), (conf, building), (conf), ())
                ORDER BY src NULLS FIRST, conf NULLS FIRST, building NULLS FIRST"""
        ).fetchall()
    finally:
        con.close()
    cols = ("source", "confidence", "building", "n", "inside_pct", "exclusive_pct", "median_area_ratio", "median_iou")
    return {
        "attribution": ATTRIBUTION,
        "min_share": MIN_SHARE,
        "groups": [_public(dict(zip(cols, r, strict=True))) for r in rows],
    }


def _public(g: dict) -> dict:
    """Published source names (as in the release's best_outline_source); parcels keep their parcel_source."""
    src = g["source"]
    if src in PUBLIC_SOURCES:
        g = {**g, "source": PUBLIC_SOURCES[src]}
        if src.endswith("_parcel"):
            g["parcel_source"] = src
    return g


def run(
    release: Path,
    parquet_dir: Path,
    work: Path,
    out: Path,
    lads: list[str] | None = None,
    skip_existing: bool = True,
    release_name: str | None = None,
) -> dict:
    """prepare (once), lad_rows per LAD (resumable), summarise; writes the summary JSON to ``out``."""
    if not (work / "osm_tags.parquet").exists() or not skip_existing:
        prepare(release, parquet_dir, work)
    avail = _lads(release)
    res: dict = {"done": [], "skipped": [], "failed": {}}
    for lad in lads or avail:
        if skip_existing and (work / "rows" / f"lad={lad}.parquet").exists():
            res["skipped"].append(lad)
            continue
        try:
            lad_rows(lad, release, parquet_dir, work)
            res["done"].append(lad)
        except Exception as exc:  # noqa: BLE001 - keep going over LADs
            res["failed"][lad] = str(exc)
    summary = summarise(work, parquet_dir)
    summary["release"] = release_name or release.name
    summary["lads"] = len(res["done"]) + len(res["skipped"])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, indent=1))
    return {**res, "summary": summary}
