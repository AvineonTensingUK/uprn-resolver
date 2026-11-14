import json
import zipfile

import duckdb
import pytest

from uprn_resolver import ros
from uprn_resolver.ros import _extract_shp, convert_ros, stage_ros


def _sq(x, y, s):
    return f"POLYGON(({x} {y},{x + s} {y},{x + s} {y + s},{x} {y + s},{x} {y}))"


def _setup(tmp_path, polys, lad="S1"):
    data = tmp_path / "data"
    raw = data / "raw"
    raw.mkdir(parents=True)
    staged = data / "staged"
    (staged / lad).mkdir(parents=True)
    con = duckdb.connect()
    con.execute("LOAD spatial")
    if polys is not None:
        (data / "parquet" / "ros").mkdir(parents=True)
        rows = ", ".join(f"('{i}', '{w}')" for i, w in polys)
        con.execute(
            f"COPY (SELECT inspire_id, ST_AsWKB(ST_GeomFromText(w)) AS geom_wkb FROM (VALUES {rows}) t(inspire_id, w)) "
            f"TO '{data}/parquet/ros/a.parquet' (FORMAT parquet)"
        )
    con.execute(
        "COPY (SELECT * FROM (VALUES (1::BIGINT,5.0,5.0,0.0,0.0),(2,6.0,6.0,0,0),(3,50.0,50.0,0,0),(4,500.0,500.0,0,0)) "
        f"t(uprn,x,y,lat,lon)) TO '{staged}/{lad}/open_uprn.parquet' (FORMAT parquet)"
    )
    return con, raw, staged


def _read(con, staged, lad="S1"):
    return {
        r[0]: r[1:]
        for r in con.execute(f"SELECT * FROM read_parquet('{staged}/{lad}/inspire_parcel.parquet')").fetchall()
    }


def test_stage_schema_and_source(tmp_path):
    big, small, other = ("a", _sq(0, 0, 20)), ("b", _sq(4, 4, 4)), ("c", _sq(40, 40, 20))
    con, raw, staged = _setup(tmp_path, [big, small, other])
    stage_ros("S1", raw, staged)
    cols = [
        r[0]
        for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet('{staged}/S1/inspire_parcel.parquet')").fetchall()
    ]
    assert cols == ["uprn", "parcel_inspire_id", "parcel_area_m2", "uprns_in_parcel", "parcel_source"]
    rows = _read(con, staged)
    assert rows[1] == ("b", pytest.approx(16.0), 2, "ros")
    assert rows[3][0] == "c" and rows[3][3] == "ros"
    assert 4 not in rows
    n = con.execute(f"SELECT count(*) FROM read_parquet('{staged}/S1/inspire_polygons.parquet')").fetchone()[0]
    assert n == 2


def test_non_scottish_lad_skipped(tmp_path):
    _, raw, staged = _setup(tmp_path, [("a", _sq(0, 0, 20))], lad="E1")
    stage_ros("E1", raw, staged)
    assert not (staged / "E1" / "inspire_parcel.parquet").exists()


def test_existing_not_clobbered(tmp_path):
    con, raw, staged = _setup(tmp_path, [("a", _sq(0, 0, 20))])
    con.execute(
        "COPY (SELECT 9::BIGINT AS uprn, 'x' AS parcel_inspire_id, 1.0 AS parcel_area_m2, 1 AS uprns_in_parcel) "
        f"TO '{staged}/S1/inspire_parcel.parquet' (FORMAT parquet)"
    )
    stage_ros("S1", raw, staged)
    assert list(_read(con, staged)) == [9]


def test_empty_existing_is_replaced(tmp_path):
    con, raw, staged = _setup(tmp_path, [("a", _sq(0, 0, 20))])
    con.execute(
        "COPY (SELECT 9::BIGINT AS uprn, 'x' AS parcel_inspire_id, 1.0 AS parcel_area_m2, 1 AS uprns_in_parcel "
        f"WHERE false) TO '{staged}/S1/inspire_parcel.parquet' (FORMAT parquet)"
    )
    stage_ros("S1", raw, staged)
    assert sorted(_read(con, staged)) == [1, 2]


def test_no_data_noop(tmp_path):
    _, raw, staged = _setup(tmp_path, None)
    stage_ros("S1", raw, staged)
    assert not (staged / "S1" / "inspire_parcel.parquet").exists()


def test_extract_shp_ignores_other_and_traversal(tmp_path):
    z = tmp_path / "X.zip"
    with zipfile.ZipFile(z, "w") as zf:
        for ext in ("shp", "shx", "dbf", "prj"):
            zf.writestr(f"XYZ_bng.{ext}", b"b")
            zf.writestr(f"XYZ_etrs89.{ext}", b"e")
        zf.writestr("../evil.shp", b"no")
    out = tmp_path / "w"
    out.mkdir()
    shp = _extract_shp(z, "XYZ", out)
    assert shp.read_bytes() == b"b"
    assert not (tmp_path / "evil.shp").exists()
    other = tmp_path / "w2"
    other.mkdir()
    with pytest.raises(ValueError):
        _extract_shp(z, "ABC", other)


def test_convert_roundtrip_manifest(tmp_path):
    pq = tmp_path / "parquet"
    (pq / "ros").mkdir(parents=True)
    con = duckdb.connect()
    con.execute("LOAD spatial")
    con.execute("SET geometry_always_xy = true")
    shp = tmp_path / "t.shp"
    con.execute(
        "COPY (SELECT 'gb.ros.lr.SCT1' AS inspireid, ST_GeomFromText('" + _sq(0, 0, 10) + "') AS geom "
        "UNION ALL SELECT 'gb.ros.lr.SCT2', ST_GeomFromText('" + _sq(20, 0, 10) + "')) "
        f"TO '{shp}' (FORMAT GDAL, DRIVER 'ESRI Shapefile', SRS 'EPSG:27700')"
    )
    out = pq / "ros" / "t.parquet"
    assert convert_ros(shp, out, pq, "T.zip") == 2
    rows = con.execute(
        f"SELECT inspire_id, ST_Area(ST_GeomFromWKB(geom_wkb)) FROM read_parquet('{out}') ORDER BY 1"
    ).fetchall()
    assert rows == [("gb.ros.lr.SCT1", 100.0), ("gb.ros.lr.SCT2", 100.0)]
    m = json.loads((pq / "manifest.json").read_text())["converted"]["ros/t.parquet"]
    assert m["licence"] == "OGL-3.0" and "Registers of Scotland" in m["attribution"] and m["rows"] == 2


def test_county_codes():
    assert len(ros.COUNTIES) == 33 and len(set(ros.COUNTIES)) == 33
