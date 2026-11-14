import json
import shutil

import duckdb
import pytest

from uprn_resolver.osm_buildings import (
    LICENCE_NAME,
    convert_osm_buildings,
    osm_outline_all,
    osm_outline_for_lad,
)
from uprn_resolver.publish import validate_geoparquet


def _sq(x, y, s):
    return f"POLYGON(({x} {y},{x + s} {y},{x + s} {y + s},{x} {y + s},{x} {y}))"


def _copy(con, sql, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    con.execute(f"COPY ({sql}) TO '{path}' (FORMAT parquet)")


def _wkb(p):
    return f"ST_AsWKB(ST_GeomFromText('{p}'))"


def _setup(tmp_path):
    con = duckdb.connect()
    con.execute("LOAD spatial")
    pq, out = tmp_path / "parquet", tmp_path / "out"
    # OSM buildings: (type, id, tag, has_addr, polygon)
    osm = [
        ("way", 1, "house", True, _sq(1000, 1000, 10)),
        ("way", 2, "apartments", False, _sq(1100, 1000, 20)),
        ("way", 3, "yes", False, _sq(1210, 1000, 10)),
        ("way", 4, "yes", False, _sq(1300, 1000, 10)),
        ("way", 5, "yes", False, _sq(1320, 1000, 10)),
        ("way", 6, "yes", False, _sq(1400, 1000, 50)),
        ("way", 7, "shed", False, _sq(1405, 1005, 5)),
    ]  # fmt: skip
    sel = " UNION ALL ".join(
        f"SELECT '{t}' osm_type, {i}::BIGINT osm_id, '{b}' building, {a} has_addr, ST_Area(g) area_m2, "
        f"ST_XMin(g) xmin, ST_YMin(g) ymin, ST_XMax(g) xmax, ST_YMax(g) ymax, ST_AsWKB(g) geom_wkb "
        f"FROM (SELECT ST_GeomFromText('{p}') g)"
        for t, i, b, a, p in osm
    )
    _copy(con, sel, pq / "osm" / "buildings.parquet")
    # UPRNs: (uprn, x, y, toid_x, toid_y, ogl_building_id, ogl_n)
    uprns = [
        (1, 1005, 1005, 1006, 1006, "g1", 1),  # toid and uprn in way/1
        (2, 1105, 1005, None, None, "g2", 8),  # uprn point only, way/2 shared with 3
        (3, 1110, 1010, 1111, 1011, "g2", 8),
        (4, 1207, 1005, None, None, "g2", 8),  # 3 m west of way/3 -> nearest_5m
        (5, 9000, 9000, None, None, None, None),  # nothing near
        (6, 1305, 1005, 1325, 1005, "g3", 2),  # toid in way/5, uprn in way/4 -> toid wins, disagree
        (7, 1407, 1007, None, None, "g4", 3),  # inside way/6 and way/7 -> smallest
        (8, None, None, None, None, None, None),  # no coordinates
    ]  # fmt: skip

    def v(x):
        return "NULL::DOUBLE" if x is None else f"{x}.0"

    def s(x):
        return "NULL::VARCHAR" if x is None else f"'{x}'"

    vals = ",".join(
        f"({u}::BIGINT,{v(x)},{v(y)},{v(tx)},{v(ty)},{s(b)},{'NULL::BIGINT' if n is None else n},"
        f"{s('Flat' if u in (2, 3) else 'House' if u in (1, 4) else None)})"
        for u, x, y, tx, ty, b, n in uprns
    )
    lad = out / "lad=E1"
    _copy(
        con,
        "SELECT uprn, x, y, toid_x, toid_y, building_id, uprns_in_building, epc_property_type FROM "
        f"(VALUES {vals}) t(uprn, x, y, toid_x, toid_y, building_id, uprns_in_building, epc_property_type)",
        lad / "uprn.parquet",
    )
    _copy(
        con,
        f"SELECT * FROM (VALUES ('g1', {_wkb(_sq(990, 990, 30))}), ('g2', {_wkb(_sq(1090, 990, 100))}),"
        f" ('g3', {_wkb(_sq(1290, 990, 50))})) t(building_id, geometry)",
        lad / "buildings.parquet",
    )
    return tmp_path / "staged", out, pq


def _run(tmp_path):
    staged, out, pq = _setup(tmp_path)
    stats = osm_outline_for_lad("E1", staged, out, pq)
    con = duckdb.connect()
    rows = con.execute(f"SELECT * FROM read_parquet('{out}/lad=E1/outline_odbl.parquet') ORDER BY uprn").fetchall()
    cols = [d[0] for d in con.description]
    return stats, {r[0]: dict(zip(cols, r, strict=True)) for r in rows}, out


def test_matching(tmp_path):
    stats, r, out = _run(tmp_path)
    assert len(r) == 8
    assert (r[1]["osm_match_method"], r[1]["osm_building_id"], r[1]["uprns_in_osm_building"]) == (
        "toid_point_in",
        "way/1",
        1,
    )
    assert r[1]["osm_building_tag"] == "house" and r[1]["osm_building_has_addr"] is True
    assert r[1]["osm_building_area_m2"] == pytest.approx(100.0)
    assert (r[2]["osm_match_method"], r[2]["osm_building_id"]) == ("uprn_point_in", "way/2")
    assert r[3]["osm_match_method"] == "toid_point_in" and r[3]["uprns_in_osm_building"] == 2
    assert r[4]["osm_match_method"] == "nearest_5m" and r[4]["osm_match_distance_m"] == pytest.approx(3.0)
    assert r[4]["osm_building_id"] == "way/3"
    assert r[5]["osm_match_method"] == "none" and r[5]["osm_building_id"] is None
    assert r[5]["uprns_in_osm_building"] is None
    assert (r[6]["osm_match_method"], r[6]["osm_building_id"], r[6]["osm_methods_disagree"]) == (
        "toid_point_in",
        "way/5",
        True,
    )
    assert r[7]["osm_building_id"] == "way/7"  # smallest containing polygon
    assert r[8]["osm_match_method"] == "none"  # no coordinates: still one row
    assert all(v["licence"] == "ODbL-1.0" for v in r.values())
    assert stats["methods"] == {"nearest_5m": 1, "none": 2, "toid_point_in": 3, "uprn_point_in": 2}
    assert (out / "lad=E1" / LICENCE_NAME).exists()


def test_comparison_fields(tmp_path):
    stats, r, _ = _run(tmp_path)
    # OGL outline g2 (100 x 100 = 10000 m2) holds 8 UPRNs; OSM way/2 (400 m2) holds 2
    assert r[2]["ogl_uprns_in_building"] == 8 and r[2]["ogl_building_area_m2"] == pytest.approx(10000.0)
    assert r[2]["osm_ogl_area_ratio"] == pytest.approx(0.04)
    assert r[2]["osm_finer_than_ogl"] is True
    assert r[1]["osm_finer_than_ogl"] is False  # 1 vs 1
    assert r[5]["osm_ogl_area_ratio"] is None and r[5]["osm_finer_than_ogl"] is None
    assert stats["osm_uprns_in_building"]["1"] == 4  # UPRNs 1, 4, 6 and 7 are each alone in their polygon
    assert stats["ogl_block_to_osm"]["2-5"] == 2  # block (>5) UPRNs 2, 3 sit in way/2 (2 UPRNs)
    assert stats["ogl_block_to_osm"]["1"] == 1  # UPRN 4 alone in way/3
    # no staged epc.parquet: property_type comes from uprn.parquet
    assert stats["ogl_block_by_property_type"]["Flat"]["osm_2_5"] == 2
    assert stats["ogl_block_by_property_type"]["House"]["osm_1"] == 1


def test_matched_polygons_are_valid_geoparquet(tmp_path):
    _, _, out = _run(tmp_path)
    p = out / "lad=E1" / "outline_odbl_buildings.parquet"
    assert validate_geoparquet(p) == []
    con = duckdb.connect()
    ids = {x[0] for x in con.execute(f"SELECT osm_building_id FROM read_parquet('{p}')").fetchall()}
    assert ids == {"way/1", "way/2", "way/3", "way/5", "way/7"}


def test_all_resumable(tmp_path):
    staged, out, pq = _setup(tmp_path)
    assert osm_outline_all(staged, out, pq)["done"] == ["E1"]
    res = osm_outline_all(staged, out, pq)
    assert res["skipped"] == ["E1"] and not res["done"]
    assert "E9" in osm_outline_all(staged, out, pq, lads=["E9"])["failed"]


OSM_XML = """<?xml version='1.0' encoding='UTF-8'?>
<osm version="0.6" generator="test">
  <node id="1" lat="51.5000" lon="-0.1000"/><node id="2" lat="51.5000" lon="-0.0999"/>
  <node id="3" lat="51.5001" lon="-0.0999"/><node id="4" lat="51.5001" lon="-0.1000"/>
  <node id="11" lat="54.6000" lon="-6.0000"/><node id="12" lat="54.6000" lon="-5.9999"/>
  <node id="13" lat="54.6001" lon="-5.9999"/><node id="14" lat="54.6001" lon="-6.0000"/>
  <node id="21" lat="51.6000" lon="-0.1000"/><node id="22" lat="51.6000" lon="-0.0999"/>
  <node id="23" lat="51.6001" lon="-0.0999"/><node id="24" lat="51.6001" lon="-0.1000"/>
  <way id="100"><nd ref="1"/><nd ref="2"/><nd ref="3"/><nd ref="4"/><nd ref="1"/>
    <tag k="building" v="house"/><tag k="addr:housenumber" v="7"/></way>
  <way id="101"><nd ref="11"/><nd ref="12"/><nd ref="13"/><nd ref="14"/><nd ref="11"/>
    <tag k="building" v="yes"/></way>
  <way id="102"><nd ref="21"/><nd ref="22"/><nd ref="23"/><nd ref="24"/><nd ref="21"/>
    <tag k="building" v="no"/></way>
</osm>
"""


@pytest.mark.skipif(shutil.which("osmium") is None or shutil.which("zstd") is None, reason="osmium/zstd missing")
def test_convert_osm_buildings(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    pbf = src / "sample.osm"
    pbf.write_text(OSM_XML)
    pq = tmp_path / "parquet"
    (pq / "osm").mkdir(parents=True)
    res = convert_osm_buildings(pbf, pq, work_dir=tmp_path / "work")
    assert res["buildings"] == 1  # NI building and building=no dropped
    con = duckdb.connect()
    con.execute("LOAD spatial")
    r = con.execute(
        f"SELECT osm_type, osm_id, building, has_addr, area_m2, xmin, xmax FROM read_parquet('{pq}/osm/buildings.parquet')"
    ).fetchone()
    assert r[:4] == ("way", 100, "house", True)
    assert 50 < r[4] < 200 and 500_000 < r[5] < r[6] < 600_000  # EPSG:27700 easting for -0.1
    m = json.loads((pq / "manifest.json").read_text())["converted"]["osm/buildings.parquet"]
    assert m["licence"] == "ODbL-1.0" and m["rows"] == 1
    assert not list((tmp_path / "work").glob("bld_*"))  # intermediates removed


def test_ni_rule_keeps_far_west_cornwall_and_scilly():
    import duckdb

    from uprn_resolver.osm_buildings import NI_RULE

    pts = {"belfast": (-5.93, 54.60), "rathlin": (-6.20, 55.29), "penzance": (-5.54, 50.12),
           "scilly": (-6.31, 49.92), "kintyre": (-5.60, 55.40), "islay": (-6.20, 55.75)}
    rows = ", ".join(f"('{k}', {lon}, {lat})" for k, (lon, lat) in pts.items())
    ni = {r[0] for r in duckdb.sql(f"SELECT n FROM (VALUES {rows}) t(n, lon, lat) WHERE {NI_RULE}").fetchall()}
    assert ni == {"belfast", "rathlin"}
