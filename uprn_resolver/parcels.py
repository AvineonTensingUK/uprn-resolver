"""Land parcel polygons for the release: the geometry behind ``parcel_inspire_id`` / a parcel ``best_outline_id``.

``parcels_for_lad`` joins the LAD's matched parcel polygons (``staged/<lad>/inspire_polygons.parquet``, written by
``inspire.stage_inspire`` for England and Wales and ``ros.stage_ros`` for Scotland) to the parcel attributes in
``out/lad=<lad>/uprn.parquet`` and writes ``out/lad=<lad>/parcels.parquet``: one row per parcel that contains at least
one UPRN of the LAD (parcel_id, parcel_source, parcel_area_m2, uprns_in_parcel, geometry in EPSG:27700).
``parcel_source`` uses the ``resolved_open.best_outline_source`` values (``inspire_parcel`` / ``ros_parcel``), so
``resolved_open`` joins on ``best_outline_id = parcel_id``. A parcel straddling two LADs appears in both files, with
the LAD-local ``uprns_in_parcel``. ``parcels_all`` loops over LADs, resumable.

Both sources are OGL v3.0. HM Land Registry's INSPIRE conditions require an Ordnance Survey statement whenever the
polygons are reused, and Registers of Scotland require theirs on any product; both are written to
``parcels.LICENCE.txt``.
"""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

from .build import _geo_meta, _tmp, write_text_atomic
from .convert import _connect, _lad, _load_spatial, _q

LICENCE_NAME = "parcels.LICENCE.txt"
HMLR_CONDITIONS = "https://use-land-property-data.service.gov.uk/datasets/inspire/#conditions"
ROS_CONDITIONS = "https://ros-inspire.themapcloud.com/"
LICENCE_TEXT = """Land parcel polygons (parcels.parquet) - licence and attribution

Licence: Open Government Licence v3.0 (https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/)

England and Wales (parcel_source = 'inspire_parcel'): HM Land Registry INSPIRE Index Polygons.
Conditions of use: {hmlr}
  This information is subject to Crown copyright and database rights {year} and is reproduced with the permission of
  HM Land Registry.
  The polygons (including the associated geometry, namely x, y co-ordinates) are subject to Crown copyright and
  database rights {year} Ordnance Survey AC0000851063.

Scotland (parcel_source = 'ros_parcel'): Registers of Scotland INSPIRE Cadastral Parcels.
Terms: {ros}
  (c) Crown copyright. Reproduced with the permission of Registers of Scotland.
  Contains OS data. (c) Crown copyright and database right {year}.

These statements must be carried with any reuse of the polygons, including in any sub-licence. A polygon shows the
indicative extent of a registered title at ground level; it is not a building outline, and the legal extent of a title
can only be established from the title plan. Only parcels containing at least one UPRN are included.
"""


def parcel_source(lad: str) -> str:
    return "ros_parcel" if lad.startswith("S") else "inspire_parcel"


def parcels_for_lad(lad: str, staged_dir: Path, out_dir: Path, parquet_dir: Path) -> dict:
    """Write ``out_dir/lad=<lad>/parcels.parquet`` (+ ``parcels.LICENCE.txt``); returns stats. An empty, schema-only
    file is written when the LAD has no staged polygons, so the table has one file per LAD."""
    lad = _lad(lad)
    out_lad = Path(out_dir) / f"lad={lad}"
    uprn = out_lad / "uprn.parquet"
    if not uprn.exists():
        raise FileNotFoundError(f"{uprn}: build the LAD first")
    polys = Path(staged_dir) / lad / "inspire_polygons.parquet"
    out = out_lad / "parcels.parquet"
    con = _connect(Path(parquet_dir))
    try:
        _load_spatial(con)
        src = (
            f"read_parquet({_q(polys)})"
            if polys.exists()
            else "(SELECT NULL::VARCHAR AS parcel_inspire_id, NULL::BLOB AS geom_wkb WHERE false)"
        )
        con.execute(
            f"""CREATE TEMP TABLE parcels AS
            SELECT a.parcel_id, {_q(parcel_source(lad))} AS parcel_source, a.parcel_area_m2, a.uprns_in_parcel,
                   ST_GeomFromWKB(g.geom_wkb) AS geom
            FROM (SELECT parcel_inspire_id AS parcel_id, any_value(parcel_area_m2) AS parcel_area_m2,
                         any_value(uprns_in_parcel) AS uprns_in_parcel
                  FROM read_parquet({_q(uprn)}) WHERE parcel_inspire_id IS NOT NULL GROUP BY 1) a
            JOIN {src} g ON g.parcel_inspire_id = a.parcel_id"""
        )
        n, x0, y0, x1, y1, types = con.execute(
            """SELECT count(*), min(ST_XMin(geom)), min(ST_YMin(geom)), max(ST_XMax(geom)), max(ST_YMax(geom)),
                      list(DISTINCT ST_GeometryType(geom)::VARCHAR) FROM parcels"""
        ).fetchone()
        gtypes = sorted({"POLYGON": "Polygon", "MULTIPOLYGON": "MultiPolygon"}.get(t, t) for t in types or [])
        geo = _geo_meta(gtypes, [x0, y0, x1, y1] if n else None).replace("'", "''")
        tmp = _tmp(out)
        tmp.unlink(missing_ok=True)
        con.execute(
            f"""COPY (SELECT * EXCLUDE (geom), ST_AsWKB(geom)::BLOB AS geometry FROM parcels ORDER BY parcel_id)
            TO {_q(tmp)} (FORMAT PARQUET, COMPRESSION ZSTD, KV_METADATA {{geo: '{geo}'}})"""
        )
        os.replace(tmp, out)
        referenced = con.execute(f"SELECT count(DISTINCT parcel_inspire_id) FROM read_parquet({_q(uprn)})").fetchone()[
            0
        ]
    finally:
        con.close()
    write_text_atomic(
        out_lad / LICENCE_NAME, LICENCE_TEXT.format(hmlr=HMLR_CONDITIONS, ros=ROS_CONDITIONS, year=dt.date.today().year)
    )
    return {"lad": lad, "parcels": n, "parcels_without_polygon": int(referenced) - n}


def parcels_all(
    staged_dir: Path, out_dir: Path, parquet_dir: Path, lads: list[str] | None = None, skip_existing: bool = True
) -> dict:
    """Run ``parcels_for_lad`` over LADs with ``out/lad=*/uprn.parquet``; resumable (skips existing files)."""
    out_dir = Path(out_dir)
    avail = sorted(p.parent.name.removeprefix("lad=") for p in out_dir.glob("lad=*/uprn.parquet"))
    res: dict = {"done": [], "skipped": [], "failed": {}}
    for lad in lads or avail:
        if lad not in avail:
            res["failed"][lad] = "no uprn.parquet"
            continue
        if skip_existing and (out_dir / f"lad={lad}" / "parcels.parquet").exists():
            res["skipped"].append(lad)
            continue
        try:
            st = parcels_for_lad(lad, staged_dir, out_dir, parquet_dir)
            if st["parcels_without_polygon"]:
                # staged polygons are out of step with uprn.parquet: do not leave a partial file to be published
                (out_dir / f"lad={lad}" / "parcels.parquet").unlink()
                res["failed"][lad] = f"{st['parcels_without_polygon']} parcel ids have no staged polygon (restage)"
            else:
                res["done"].append(lad)
        except Exception as exc:  # noqa: BLE001 - keep going over LADs
            res["failed"][lad] = str(exc)
    return res
