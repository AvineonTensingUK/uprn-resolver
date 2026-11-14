import json

import duckdb
import pyarrow.parquet as pq

from uprn_resolver import ni
from uprn_resolver.ni import LICENCE_NAME, OUT_NAME, _convert_features, ni_address_points

BOUNDARY = {"type": "Polygon", "coordinates": [[[-6.0, 54.0], [-5.0, 54.0], [-5.0, 55.0], [-6.0, 55.0], [-6.0, 54.0]]]}


def _sq(lon, lat, d=0.0004):
    return {
        "type": "Polygon",
        "coordinates": [[[lon, lat], [lon + d, lat], [lon + d, lat + d], [lon, lat + d], [lon, lat]]],
    }


def _feat(fid, geom, **tags):
    return {"type": "Feature", "id": fid, "properties": tags, "geometry": geom}


def _con():
    con = duckdb.connect()
    con.execute("LOAD spatial")
    return con


def test_convert_features_clips_and_geoparquet(tmp_path):
    feats = [
        _feat("w1", _sq(-5.5, 54.5), building="house", **{"addr:housenumber": "1", "addr:street": "High Street"}),
        _feat("w2", _sq(-5.5, 54.501), building="yes"),
        _feat("w3", _sq(-7.5, 54.5), building="house"),  # outside the boundary (Republic side)
        _feat("w4", _sq(-5.5, 54.502), building="no"),  # not a building
        _feat("n5", {"type": "Point", "coordinates": [-5.4, 54.4]}, **{"addr:housename": "Rose Cottage"}),
        _feat(
            "w6",
            {"type": "LineString", "coordinates": [[-5.5, 54.4], [-5.5, 54.45]]},
            highway="residential",
            name="High Street",
        ),
        _feat(
            "w7",
            {"type": "LineString", "coordinates": [[-7.5, 54.4], [-7.4, 54.4]]},
            highway="residential",
            name="Far Road",
        ),
        _feat("w8", {"type": "LineString", "coordinates": [[-5.6, 54.4], [-5.65, 54.4]]}, highway="service"),  # unnamed
    ]
    seq = tmp_path / "f.geojsonseq"
    seq.write_text("\n".join(json.dumps(f) for f in feats))
    bnd = tmp_path / "b.geojson"
    bnd.write_text(json.dumps(BOUNDARY))
    con = _con()
    out = tmp_path / "ni"
    counts = _convert_features(con, seq, bnd, out)
    assert counts == {"buildings": 2, "addresses": 2, "highways": 1}
    meta = json.loads(pq.read_schema(out / "buildings.parquet").metadata[b"geo"])
    assert meta["version"] == "1.1.0" and meta["primary_column"] == "geometry"
    assert meta["columns"]["geometry"]["encoding"] == "WKB"
    assert meta["columns"]["geometry"]["crs"]["id"]["code"] == 29903
    x, y = con.execute(f"SELECT x, y FROM '{out / 'addresses.parquet'}' WHERE housename = 'Rose Cottage'").fetchone()
    assert 300000 < x < 400000 and 300000 < y < 400000  # Irish Grid, not lon/lat
    lon, lat = con.execute(
        f"SELECT lon, lat FROM '{out / 'addresses.parquet'}' WHERE housename = 'Rose Cottage'"
    ).fetchone()
    assert abs(lon + 5.4) < 1e-9 and abs(lat - 54.4) < 1e-9


def _write(con, sql, path, geoms):
    path.parent.mkdir(parents=True, exist_ok=True)
    return ni._copy_geo(con, sql, path, geoms)


def _poly_wkb(x, y, s):
    return f"ST_AsWKB(ST_GeomFromText('POLYGON(({x} {y},{x + s} {y},{x + s} {y + s},{x} {y + s},{x} {y}))'))"


def _pt(x, y):
    return f"ST_AsWKB(ST_GeomFromText('POINT({x} {y})'))"


def _setup(tmp_path):
    con = _con()
    pq_dir = tmp_path / "parquet"
    g = {"geometry": "grid", "geometry_wgs84": "wgs84"}
    b = [
        # osm_type, id, building, street, postcode, x, y, size
        ("way", 1, "house", None, None, 1000, 1000, 20),  # own building, address node inside too
        ("way", 2, "flats", "Tagged Rd", "BT1 1AA", 1100, 1000, 40),  # building tags carry street+postcode
        ("way", 3, "yes", None, None, 1200, 1000, 40),  # big, contains small
        ("way", 4, "yes", None, None, 1210, 1010, 10),  # small, nested in 3
    ]
    sel = " UNION ALL ".join(
        f"SELECT '{t}' osm_type, {i}::BIGINT osm_id, '{bd}' building, NULL name, NULL housenumber, NULL housename, "
        f"{'NULL' if s is None else repr(s)} street, {'NULL' if p is None else repr(p)} postcode, NULL city, "
        f"{sz * sz}.0 area_m2, 0.0 lon, 0.0 lat, 0.0 x, 0.0 y, 0.0 xmin, 0.0 ymin, 0.0 xmax, 0.0 ymax, "
        f"{_poly_wkb(x, y, sz)} geometry, {_poly_wkb(x, y, sz)} geometry_wgs84"
        for t, i, bd, s, p, x, y, sz in b
    )
    _write(con, sel, pq_dir / "ni" / "buildings.parquet", g)
    a = [
        # type, id, hn, street, postcode, is_building, x, y
        ("way", 1, "1", "Main St", "BT1 2BB", True, 1010, 1010),
        ("node", 10, "2", None, None, False, 1110, 1010),  # in building 2 -> street+postcode from building
        ("node", 11, "3", None, None, False, 1215, 1015),  # nested -> smallest building (4), ambiguous
        ("node", 12, "4", None, None, False, 1500, 1020),  # no building; 20 m from Near Lane
        ("node", 13, "5", None, None, False, 5000, 5000),  # nothing nearby
    ]
    sel = " UNION ALL ".join(
        f"SELECT '{t}' osm_type, {i}::BIGINT osm_id, '{hn}' housenumber, NULL housename, NULL unit, "
        f"{'NULL' if s is None else repr(s)} street, {'NULL' if p is None else repr(p)} postcode, NULL city, "
        f"{str(ib).upper()} is_building, 0.0 lon, 0.0 lat, {x}.0 x, {y}.0 y, {_pt(x, y)} geometry, {_pt(x, y)} geometry_wgs84"
        for t, i, hn, s, p, ib, x, y in a
    )
    _write(con, sel, pq_dir / "ni" / "addresses.parquet", g)
    hw = (
        "SELECT 1::BIGINT way_id, 'residential' highway, 'Near Lane' name, 1400.0 xmin, 1000.0 ymin, 1600.0 xmax, "
        "1000.0 ymax, ST_AsWKB(ST_GeomFromText('LINESTRING(1400 1000,1600 1000)')) geometry, "
        "ST_AsWKB(ST_GeomFromText('LINESTRING(1400 1000,1600 1000)')) geometry_wgs84"
    )
    _write(con, hw, pq_dir / "ni" / "highways.parquet", g)
    return con, pq_dir


def test_ni_address_points(tmp_path):
    con, pq_dir = _setup(tmp_path)
    out_dir = tmp_path / "out"
    stats = ni_address_points(pq_dir, out_dir)
    assert stats["addresses"] == 5
    out = out_dir / "ni" / OUT_NAME
    rows = {
        r[0]: r
        for r in con.execute(
            f"""SELECT osm_id, building_match, building_osm_id, street, street_source, postcode, postcode_source,
                       n_buildings_containing, confidence, street_distance_m FROM '{out}'"""
        ).fetchall()
    }
    assert rows[1][1:3] == ("own_building", 1)
    assert rows[1][3:7] == ("Main St", "osm_addr", "BT1 2BB", "osm_addr") and rows[1][8] == "high"
    assert rows[10][1:7] == ("inside_building", 2, "Tagged Rd", "building_addr", "BT1 1AA", "building_addr")
    assert rows[11][1:3] == ("inside_building", 4) and rows[11][7] == 2  # smallest of the nested pair
    assert rows[11][8] == "medium"
    assert rows[12][1] == "none" and rows[12][3:5] == ("Near Lane", "nearest_highway_50m") and rows[12][8] == "low"
    assert abs(rows[12][9] - 20.0) < 1e-6
    assert rows[13][3] is None and rows[13][4] is None and rows[13][8] == "low"
    assert stats["with_building"] == 3 and stats["buildings_with_address"] == 3
    # GeoParquet + no UPRN column anywhere + ODbL sidecar
    cols = [f.name for f in pq.read_schema(out)]
    assert not any("uprn" in c.lower() for c in cols)
    assert json.loads(pq.read_schema(out).metadata[b"geo"])["primary_column"] == "geometry"
    lic = (out_dir / "ni" / LICENCE_NAME).read_text()
    assert "ODbL" in lic and "NO UPRNs" in lic


def _setup_ms(tmp_path):
    con, pq_dir = _setup(tmp_path)
    g = {"geometry": "grid"}
    ms = [  # ms_id, x, y, size
        ("msA", 1001, 1001, 20),  # ~ OSM building 1 (IoU ~0.9) and contains address 1
        ("msB", 1120, 1000, 40),  # overlaps OSM building 2 (x 1100-1140) by 20x40: IoU = 800/2400 = 1/3 -> no match
        ("msC", 3000, 3000, 15),  # no OSM at all
        ("msD", 1200, 1000, 40),  # == OSM building 3
    ]
    sel = " UNION ALL ".join(
        f"SELECT '{i}' ms_id, {_poly_wkb(x, y, s)} geometry, {s * s}.0 area_m2, 0.0 lon_min, 0.0 lat_min, "
        f"1.0 lon_max, 1.0 lat_max"
        for i, x, y, s in ms
    )
    _write(con, sel, pq_dir / "msbuildings" / "ni" / "t1.parquet", g)
    return con, pq_dir


def test_ni_address_points_adds_ms_columns(tmp_path):
    con, pq_dir = _setup_ms(tmp_path)
    out_dir = tmp_path / "out"
    stats = ni_address_points(pq_dir, out_dir)
    assert stats["with_ms_building"] == 2  # addresses 1 (msA) and 11 (msD)
    rows = {
        r[0]: r[1:]
        for r in con.execute(f"SELECT osm_id, ms_building_id, ms_area_m2 FROM '{out_dir / 'ni' / OUT_NAME}'").fetchall()
    }
    assert rows[1] == ("msA", 400.0)
    assert rows[10] == (None, None)  # (1110, 1010) is outside msB (starts 1120)
    assert rows[11] == ("msD", 1600.0)
    assert rows[13] == (None, None)


def test_ni_ms_coverage(tmp_path):
    con, pq_dir = _setup_ms(tmp_path)
    out_dir = tmp_path / "out"
    ni_address_points(pq_dir, out_dir)
    st = ni.ni_ms_coverage(pq_dir, out_dir)
    assert st["osm_buildings"] == 4 and st["ms_buildings"] == 4
    assert st["osm_with_ms_counterpart"] == 2  # buildings 1 and 3
    assert st["osm_overlapping_any_ms"] == 4  # + building 2 (msB partial) and nested building 4 (inside msD)
    assert st["ms_with_osm_counterpart"] == 2 and st["ms_without_osm_counterpart"] == 2
    assert st["ms_with_no_osm_overlap_at_all"] == 1  # msC only
    assert st["osm_addressed_buildings"] == 3 and st["osm_addressed_buildings_with_ms_counterpart"] == 1
    f = out_dir / "ni" / ni.MS_UNMATCHED_NAME
    rows = {r[0]: r[1:] for r in con.execute(f"SELECT ms_id, overlaps_osm, licence FROM '{f}'").fetchall()}
    assert set(rows) == {"msB", "msC"}
    assert rows["msB"][0] is True and rows["msC"][0] is False and rows["msC"][1] == "CDLA-Permissive-2.0"
    meta = json.loads(pq.read_schema(f).metadata[b"geo"])
    assert meta["columns"]["geometry"]["crs"]["id"]["code"] == 29903
    assert json.loads((out_dir / "ni" / ni.MS_STATS_NAME).read_text())["unmatched_rows"] == 2
