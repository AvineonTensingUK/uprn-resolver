import json

import duckdb
import pytest

from uprn_resolver.convert import convert


def _rows(path, sql="*"):
    return duckdb.connect().execute(f"SELECT {sql} FROM read_parquet('{path}')").fetchall()


@pytest.fixture
def pq(tmp_path):
    d = tmp_path / "parquet"
    d.mkdir()
    return d


def _lids(tmp_path):
    f = tmp_path / "lids.csv"
    f.write_text(
        "﻿CORRELATION_ID,IDENTIFIER_1,VERSION_NUMBER_1,VERSION_DATE_1,IDENTIFIER_2,VERSION_NUMBER_2,VERSION_DATE_2,CONFIDENCE\n"
        "a,100,,20251126000000,osgb1,8,20150409000000,Version information is correct\n"
        "b,101,,20251126000000,osgb1,8,20150409000000,Version information is correct\n"
        "c,102,3,20251126000000,osgb2,8,20150409000000,Other\n"
    )
    return f


def test_lids(tmp_path, pq):
    assert convert("lids", _lids(tmp_path), pq) == 3
    rows = _rows(pq / "lids" / "lids.parquet", "uprn, toid, lids_confidence")
    assert rows[0] == (100, "osgb1", "Version information is correct")
    manifest = json.loads((pq / "manifest.json").read_text())
    assert manifest["converted"]["lids/lids.parquet"]["rows"] == 3


def test_open_uprn(tmp_path, pq):
    f = tmp_path / "u.csv"
    f.write_text("﻿UPRN,X_COORDINATE,Y_COORDINATE,LATITUDE,LONGITUDE\n1,358260.99,172796.83,51.45,-2.6\n26,1,2,3,4\n")
    assert convert("open_uprn", f, pq) == 2
    assert _rows(pq / "open_uprn" / "open_uprn.parquet")[0] == (1, 358260.99, 172796.83, 51.45, -2.6)


def test_open_toid_semi_join(tmp_path, pq):
    convert("lids", _lids(tmp_path), pq)
    f = tmp_path / "osopentoid_202609_hp.csv"
    f.write_text(
        "﻿TOID,VERSION_NUMBER,VERSION_DATE,SOURCE_PRODUCT,EASTING,NORTHING\n"
        "osgb1,2,20060301,OS,450867.727,1204759.234\n"
        "osgb2,2,20060301,OS,1.5,2.5\n"
        "osgb999,2,20060301,OS,3,4\n"
    )
    assert convert("open_toid", f, pq) == 2
    rows = _rows(pq / "open_toid" / "hp.parquet", "toid, toid_x, toid_y")
    assert sorted(r[0] for r in rows) == ["osgb1", "osgb2"]


def test_open_toid_requires_lids(tmp_path, pq):
    f = tmp_path / "osopentoid_202609_hp.csv"
    f.write_text("TOID,EASTING,NORTHING\nx,1,2\n")
    with pytest.raises(FileNotFoundError):
        convert("open_toid", f, pq)


def test_onsud_column_variants(tmp_path, pq):
    f = tmp_path / "onsud.csv"
    f.write_text(
        "UPRN,GRIDGB1E,GRIDGB1N,PCDS,OA21CD,LSOA21CD,MSOA21CD,LAD25CD\n"
        "10,1,2,AB1 2CD,E00000001,E01000001,E02000001,E09000019\n"
    )
    assert convert("onsud", f, pq) == 1
    assert _rows(pq / "onsud" / "onsud.parquet") == [
        (10, "E09000019", "E00000001", "E01000001", "E02000001", "AB1 2CD")
    ]


def test_unknown_dataset(tmp_path, pq):
    with pytest.raises(ValueError):
        convert("nope", tmp_path, pq)


def test_openmap_local(tmp_path, pq):
    con = duckdb.connect()
    try:
        con.execute("LOAD spatial")
    except duckdb.Error:
        pytest.skip("duckdb spatial extension unavailable")
    d = tmp_path / "sz"
    d.mkdir()
    shp = d / "SZ_Building.shp"
    con.execute(
        f"COPY (SELECT ST_GeomFromText('POLYGON((0 0,0 1,1 1,1 0,0 0))') AS geom UNION ALL "
        f"SELECT ST_GeomFromText('POLYGON((5 5,5 6,6 6,6 5,5 5))')) TO '{shp}' (FORMAT GDAL, DRIVER 'ESRI Shapefile')"
    )
    assert convert("openmap_local", shp, pq) == 2
    rows = _rows(pq / "openmap_local" / "sz.parquet", "building_id, geom_wkb")
    assert len({r[0] for r in rows}) == 2 and all(len(r[0]) == 40 for r in rows)


def test_corrupt_manifest_raises(tmp_path, pq):
    (pq / "manifest.json").write_text("{not json")
    with pytest.raises(RuntimeError, match="corrupt manifest"):
        convert("lids", _lids(tmp_path), pq)
    assert (pq / "manifest.json").read_text() == "{not json"  # left untouched


def test_lad_validation():
    from uprn_resolver.convert import _lad

    assert _lad("E09000019") == "E09000019" and _lad("ni") == "ni" and _lad("S12000033") == "S12000033"
    for bad in ("E1", "E09000019'; DROP", "e09000019", "../x", "N09000001", "E090000190"):
        with pytest.raises(ValueError):
            _lad(bad)
