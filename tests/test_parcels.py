import json
from pathlib import Path

import duckdb
import pyarrow.parquet as pq
import pytest

from uprn_resolver import parcels, publish

try:
    duckdb.connect().execute("LOAD spatial")
except Exception:  # pragma: no cover
    pytest.skip("duckdb spatial extension unavailable", allow_module_level=True)

SQUARE = "POLYGON((0 0,10 0,10 10,0 10,0 0))"


def _lad(tmp_path: Path, lad: str, polygons: dict[str, str]) -> tuple[Path, Path, Path]:
    """uprn.parquet with UPRNs 1, 2 in parcel p1, 3 in p2 and 4 in none; staged polygons as given."""
    staged, out, pqd = tmp_path / "staged", tmp_path / "out", tmp_path / "parquet"
    (staged / lad).mkdir(parents=True)
    (out / f"lad={lad}").mkdir(parents=True)
    pqd.mkdir(exist_ok=True)
    con = duckdb.connect()
    con.execute("LOAD spatial")
    con.execute(
        f"""COPY (SELECT * FROM (VALUES (1, 'p1', 100.0, 2), (2, 'p1', 100.0, 2), (3, 'p2', 50.0, 1),
        (4, NULL, NULL, NULL)) t(uprn, parcel_inspire_id, parcel_area_m2, uprns_in_parcel))
        TO '{out}/lad={lad}/uprn.parquet' (FORMAT parquet)"""
    )
    rows = ", ".join(f"('{k}', ST_AsWKB(ST_GeomFromText('{v}')))" for k, v in polygons.items())
    con.execute(
        f"""COPY (SELECT * FROM (VALUES {rows}) t(parcel_inspire_id, geom_wkb))
        TO '{staged}/{lad}/inspire_polygons.parquet' (FORMAT parquet)"""
    )
    return staged, out, pqd


def test_parcels_join_source_and_geo_metadata(tmp_path):
    staged, out, pqd = _lad(tmp_path, "E09000001", {"p1": SQUARE, "p2": SQUARE, "unused": SQUARE})
    st = parcels.parcels_for_lad("E09000001", staged, out, pqd)
    assert st == {"lad": "E09000001", "parcels": 2, "parcels_without_polygon": 0}
    f = out / "lad=E09000001" / "parcels.parquet"
    rows = duckdb.sql(
        f"SELECT parcel_id, parcel_source, parcel_area_m2, uprns_in_parcel FROM '{f}' ORDER BY 1"
    ).fetchall()
    assert rows == [("p1", "inspire_parcel", 100.0, 2), ("p2", "inspire_parcel", 50.0, 1)]
    geo = json.loads(pq.read_schema(f).metadata[b"geo"])
    assert geo["columns"]["geometry"]["geometry_types"] == ["Polygon"]
    assert geo["columns"]["geometry"]["bbox"] == [0.0, 0.0, 10.0, 10.0]
    assert publish.validate_parquet(f, ogl=True) == []
    lic = (out / "lad=E09000001" / parcels.LICENCE_NAME).read_text()
    assert "AC0000851063" in lic and "Registers of Scotland" in lic and "ODbL" not in lic


def test_scottish_lad_is_ros_and_missing_polygon_is_reported(tmp_path):
    staged, out, pqd = _lad(tmp_path, "S12000001", {"p1": SQUARE})
    st = parcels.parcels_for_lad("S12000001", staged, out, pqd)
    assert st["parcels"] == 1 and st["parcels_without_polygon"] == 1
    f = out / "lad=S12000001" / "parcels.parquet"
    assert duckdb.sql(f"SELECT DISTINCT parcel_source FROM '{f}'").fetchall() == [("ros_parcel",)]
    res = parcels.parcels_all(staged, out, pqd, skip_existing=False)
    assert "S12000001" in res["failed"] and not res["done"]
    assert not f.exists()  # a partial file is not left behind, so a re-run retries the LAD
    assert "S12000001" in parcels.parcels_all(staged, out, pqd)["failed"]


def test_no_staged_polygons_writes_empty_file(tmp_path):
    staged, out, pqd = _lad(tmp_path, "W06000001", {"p1": SQUARE})
    (staged / "W06000001" / "inspire_polygons.parquet").unlink()
    st = parcels.parcels_for_lad("W06000001", staged, out, pqd)
    assert st["parcels"] == 0 and st["parcels_without_polygon"] == 2
    assert pq.read_schema(out / "lad=W06000001" / "parcels.parquet").names == [
        "parcel_id", "parcel_source", "parcel_area_m2", "uprns_in_parcel", "geometry",
    ]  # fmt: skip


def test_parcels_is_a_released_ogl_geo_table():
    assert "parcels" in publish.OPTIONAL_GEO and "parcels" in publish.OGL_TABLES
    assert publish.LICENCE_SIDECAR["parcels"] == parcels.LICENCE_NAME
