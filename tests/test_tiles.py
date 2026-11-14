import json
import shutil
from pathlib import Path

import duckdb
import numpy as np
import pytest
import shapely

from uprn_resolver import bng, tiles

try:
    bng.grid_path()
except FileNotFoundError:  # pragma: no cover
    pytest.skip("OSTN15 grid not downloaded (data/proj)", allow_module_level=True)

try:
    duckdb.connect().execute("LOAD spatial")
except Exception:  # pragma: no cover
    pytest.skip("duckdb spatial extension unavailable", allow_module_level=True)


def _release(tmp_path: Path) -> Path:
    con = duckdb.connect()
    con.execute("LOAD spatial")
    d = tmp_path / "rel"
    (d / "resolved_open" / "lad=E09000001").mkdir(parents=True)
    (d / "outline_odbl_buildings" / "lad=E09000001").mkdir(parents=True)
    con.execute(
        f"""COPY (SELECT 1::BIGINT AS uprn, 530624.97 AS x, 178388.46 AS y, 'high' AS outline_confidence,
        'os' AS best_outline_source, 'High St' AS street_name, NULL::VARCHAR AS locality
        UNION ALL SELECT 2, NULL, NULL, NULL, NULL, NULL, NULL)
        TO '{d}/resolved_open/lad=E09000001/resolved_open.parquet' (FORMAT parquet)"""
    )
    con.execute(
        f"""COPY (SELECT 'w1' AS osm_building_id, 'house' AS osm_building_tag, 55.55::DOUBLE AS osm_building_area_m2,
        ST_GeomFromText('POLYGON((530600 178300,530620 178300,530620 178320,530600 178320,530600 178300))') AS geometry)
        TO '{d}/outline_odbl_buildings/lad=E09000001/outline_odbl_buildings.parquet' (FORMAT parquet)"""
    )
    return d


def test_points_are_wgs84_and_skip_null_coords(tmp_path):
    d = _release(tmp_path)
    (f,) = tiles.lad_files(d, "resolved_open", "E09")
    feats = [json.loads(s) for s in tiles.features(tiles.KINDS["points"], f)]
    assert len(feats) == 1
    lon, lat = feats[0]["geometry"]["coordinates"]
    assert (lon, lat) == pytest.approx((-0.1199256, 51.4893656), abs=2e-6)  # OS test point TP09
    assert feats[0]["properties"] == {
        "uprn": 1, "outline_confidence": "high", "best_outline_source": "os", "street_name": "High St",
    }  # fmt: skip


def test_polygons_are_wgs84_roundtrip(tmp_path):
    d = _release(tmp_path)
    (f,) = tiles.lad_files(d, "outline_odbl_buildings")
    (feat,) = [json.loads(s) for s in tiles.features(tiles.KINDS["odbl"], f)]
    ring = np.array(feat["geometry"]["coordinates"][0])
    x, y = bng.wgs84_to_bng(ring[:, 0], ring[:, 1])
    assert (
        shapely.box(530600, 178300, 530620, 178320).symmetric_difference(shapely.Polygon(np.column_stack([x, y]))).area
        < 0.5
    )
    assert feat["properties"]["osm_building_area_m2"] == pytest.approx(55.6, abs=0.1)


def test_licences_not_mixed():
    tables = [k.table for k in tiles.KINDS.values()]
    assert len(set(tables)) == len(tables)
    odbl = {"odbl", "ni"}
    for name in odbl:
        assert "ODbL" in tiles.KINDS[name].attribution
    for name, k in tiles.KINDS.items():
        if name not in odbl:
            assert "ODbL" not in k.attribution
    assert not any("odbl" in k.table or k.table.startswith("ni_") for n, k in tiles.KINDS.items() if n not in odbl)


def test_ni_points_are_lonlat_from_data_dir(tmp_path):
    con = duckdb.connect()
    (tmp_path / "out" / "ni").mkdir(parents=True)
    con.execute(
        f"""COPY (SELECT 7::BIGINT AS osm_id, 'Main St' AS street, NULL::VARCHAR AS city, 'high' AS confidence,
        'contains' AS building_match, -5.9301 AS lon, 54.5973 AS lat)
        TO '{tmp_path}/out/ni/ni_address_buildings.parquet' (FORMAT parquet)"""
    )
    rel = tmp_path / "release" / "2026-10"
    (f,) = tiles.kind_files(rel, tiles.KINDS["ni"], prefix="E09")
    (feat,) = [json.loads(s) for s in tiles.features(tiles.KINDS["ni"], f)]
    assert feat["geometry"]["coordinates"] == [-5.9301, 54.5973]
    assert feat["properties"] == {"osm_id": 7, "street": "Main St", "confidence": "high", "building_match": "contains"}


def test_disk_guard(tmp_path, monkeypatch):
    monkeypatch.setattr(tiles, "free_bytes", lambda p: 1)
    with pytest.raises(RuntimeError):
        tiles.check_disk(tmp_path)


@pytest.mark.skipif(shutil.which("tippecanoe") is None, reason="tippecanoe not installed")
def test_build_archive(tmp_path):
    d = _release(tmp_path)
    r = tiles.build(d, "points", prefix="E09", out_dir=tmp_path / "out", tmp_dir=tmp_path / "t")
    assert r["features"] == 1
    assert Path(r["archive"]).read_bytes()[:7] == b"PMTiles"


def test_snap_repairs_invalid_polygons():
    bowtie = shapely.Polygon([(0, 0), (1, 1), (1, 0), (0, 1), (0, 0)])  # self-intersecting
    hole_outside = shapely.Polygon([(0, 0), (1, 0), (1, 1), (0, 1)], holes=[[(2, 2), (3, 2), (3, 3), (2, 3)]])
    out = tiles._snap(np.array([bowtie, hole_outside, shapely.box(0.123456789, 0, 1, 1)], dtype=object))
    assert len(out) == 3 and all(g is not None and not g.is_empty for g in out)
    assert shapely.get_coordinates(out[2]).min() == pytest.approx(0.0)
