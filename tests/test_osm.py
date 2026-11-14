import duckdb

from uprn_resolver.osm import (
    CONFIDENCE,
    LICENCE_NAME,
    _osm_number_set,
    normalise_name,
    osm_all,
    osm_for_lad,
)


def _sq(x, y, s):
    return f"POLYGON(({x} {y},{x + s} {y},{x + s} {y + s},{x} {y + s},{x} {y}))"


def _copy(con, sql, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    con.execute(f"COPY ({sql}) TO '{path}' (FORMAT parquet)")


def _line(x0, y0, x1, y1):
    return f"ST_AsWKB(ST_GeomFromText('LINESTRING({x0} {y0},{x1} {y1})'))"


def geom(p):
    return "NULL::BLOB" if p is None else "ST_AsWKB(ST_GeomFromText('" + p + "'))"


def _setup(tmp_path):
    con = duckdb.connect()
    con.execute("LOAD spatial")
    pq, staged = tmp_path / "parquet", tmp_path / "staged"
    uprns = {
        1: (1000, 1000), 2: (1100, 1000), 3: (1200, 1000), 4: (1204, 1000), 5: (1300, 1000),
        6: (1400, 1000), 7: (1404, 1000), 8: (1500, 1000), 9: (1600, 1000), 10: (1700, 1000),
    }  # fmt: skip
    vals = ",".join(f"({u}::BIGINT,{x}.0,{y}.0)" for u, (x, y) in uprns.items())
    _copy(con, f"SELECT * FROM (VALUES {vals}) t(uprn,x,y)", staged / "E1" / "open_uprn.parquet")
    _copy(con, f"SELECT uprn, 'E1' lad FROM (VALUES {','.join(f'({u}::BIGINT)' for u in uprns)}) t(uprn)",
          staged / "E1" / "onsud.parquet")  # fmt: skip
    _copy(
        con,
        "SELECT * FROM (VALUES (1::BIGINT,'High Street',NULL),(9,'Other Road',NULL),(8,'Nowhere Lane',NULL))"
        " t(uprn,street_name,street_name_alt)",
        staged / "E1" / "streets.parquet",
    )
    hw = [
        (1, "High St", _line(900, 1010, 1100, 1010)),
        (2, "Wrong Rd", _line(1590, 1005, 1610, 1005)),
        (3, "Other Rd", _line(1560, 1030, 1640, 1030)),
        (4, "Far Street", _line(1000, 1200, 1100, 1200)),
    ]
    sel = " UNION ALL ".join(
        f"SELECT {i}::BIGINT way_id, 'residential' highway, '{n}' name, NULL name_cy, NULL name_gd, "
        f"ST_XMin(g) xmin, ST_YMin(g) ymin, ST_XMax(g) xmax, ST_YMax(g) ymax, ST_AsWKB(g) geom_wkb "
        f"FROM (SELECT ST_GeomFromWKB({w}) g)"
        for i, n, w in hw
    )
    _copy(con, sel, pq / "osm" / "highways.parquet")
    # addresses: (type, id, number, ref, is_building, x, y, polygon)
    rows = [
        ("node", 100, "1", "1", False, 1002.0, 1001.0, None),  # ref to UPRN 1
        ("way", 101, "2", None, True, 1095.0, 995.0, _sq(1095, 995, 10)),  # contains only UPRN 2
        ("way", 102, "3", None, True, 1195.0, 995.0, _sq(1195, 995, 15)),  # contains UPRNs 3 and 4
        ("node", 103, "5", None, False, 1303.0, 1000.0, None),  # mutual nearest with UPRN 5
        ("node", 104, "6", None, False, 1401.0, 1000.0, None),  # nearest to 6, mutual
        ("node", 105, "8", None, False, 1501.0, 1000.0, None),  # 8's nearest node is 106 -> not mutual
        ("node", 106, "9", None, False, 1500.5, 1000.0, None),
        ("way", 107, "10", "1;10", True, 1695.0, 995.0, _sq(1695, 995, 10)),  # ref list: UPRN 10 AND 1 (exact only)
    ]  # fmt: skip
    sel = " UNION ALL ".join(
        f"SELECT '{t}' osm_type, {i}::BIGINT osm_id, '{n}' housenumber, NULL housename, 'High Street' street, "
        f"NULL unit, NULL flats, 'N1 1AA' postcode, NULL city, "
        f"{'NULL' if r is None else repr(r)} ref_gb_uprn, {b} is_building, {x} x, {y} y, "
        f"{geom(p)} geom_wkb"
        for t, i, n, r, b, x, y, p in rows
    )
    _copy(con, sel, pq / "osm" / "addresses.parquet")
    return staged, tmp_path / "out", pq


def _run(tmp_path):
    staged, out, pq = _setup(tmp_path)
    stats = osm_for_lad("E1", staged, out, pq)
    con = duckdb.connect()
    rows = con.execute(f"SELECT * FROM read_parquet('{out}/lad=E1/streets_odbl.parquet') ORDER BY uprn").fetchall()
    cols = [d[0] for d in con.description]
    return stats, {r[0]: dict(zip(cols, r, strict=True)) for r in rows}, out


def test_normalise():
    assert normalise_name("St. John's Rd") == "street johns road"
    assert normalise_name("High St") == normalise_name("high street")
    assert normalise_name("The Mall") == "mall"
    assert normalise_name(None) is None


def test_addresses(tmp_path):
    stats, r, out = _run(tmp_path)
    assert len(r) == 10
    # exact ref wins, even though the ref tag sits on a node 2 m away
    assert (r[1]["osm_addr_match_method"], r[1]["osm_housenumber"]) == ("ref_gb_uprn", "1")
    # single-UPRN building
    assert (r[2]["osm_addr_match_method"], r[2]["osm_housenumber"]) == ("inside_building_single", "2")
    # building with two UPRNs: never assigned
    assert r[3]["osm_addr_match_method"] is None and r[4]["osm_addr_match_method"] is None
    # mutual nearest
    assert (r[5]["osm_addr_match_method"], r[5]["osm_housenumber"]) == ("nearest_node_10m", "5")
    assert (r[6]["osm_addr_match_method"], r[6]["osm_housenumber"]) == ("nearest_node_10m", "6")
    assert r[7]["osm_addr_match_method"] is None
    # UPRN 8: nearest node is 106 (0.5 m) but that node's nearest UPRN is 8 -> mutual, assigned; node 105 dropped
    assert (r[8]["osm_addr_match_method"], r[8]["osm_housenumber"]) == ("nearest_node_10m", "9")
    # ref list tag: UPRN 10 matches exactly; UPRN 1 already had a better (numbered) match so keeps its own
    assert r[10]["osm_addr_match_method"] == "ref_gb_uprn" and r[10]["osm_housenumber"] == "10"
    # no OSM object is used by two UPRNs except via ref_gb_uprn
    used = [(v["osm_addr_osm_type"], v["osm_addr_osm_id"]) for v in r.values()
            if v["osm_addr_match_method"] not in (None, "ref_gb_uprn")]  # fmt: skip
    assert len(used) == len(set(used))
    assert stats["ref_gb_uprn"] == 2
    assert all(v["licence"] == "ODbL-1.0" for v in r.values())
    assert (out / "lad=E1" / LICENCE_NAME).exists()


def test_streets(tmp_path):
    _, r, _ = _run(tmp_path)
    assert r[1]["osm_street_name"] == "High St" and r[1]["osm_agrees_os"] is True
    assert r[1]["osm_street_distance_m"] == 10.0
    # no OS name -> OSM name still assigned, agreement NULL
    assert r[2]["osm_street_name"] == "High St" and r[2]["osm_agrees_os"] is None
    # prefers the way matching the OS name (30 m) over the nearer one (5 m); nearest disagrees
    assert r[9]["osm_street_name"] == "Other Rd" and r[9]["osm_agrees_os"] is False
    # nothing within 50 m
    assert r[3]["osm_street_name"] is None and r[8]["osm_street_name"] is None


def test_osm_all_resumable(tmp_path):
    staged, out, pq = _setup(tmp_path)
    assert osm_all(staged, out, pq)["done"] == ["E1"]
    again = osm_all(staged, out, pq)
    assert again["skipped"] == ["E1"] and not again["failed"]


def test_confidence_column(tmp_path):
    _, r, _ = _run(tmp_path)
    for u in (1, 2, 5, 10):
        assert (
            r[u]["osm_addr_confidence"] == CONFIDENCE[r[u]["osm_addr_match_method"]]
            and r[u]["building_level_address"] is False
        )
    assert r[3]["osm_addr_confidence"] is None and r[7]["osm_addr_confidence"] is None
    assert set(CONFIDENCE.values()) <= {"high", "medium", "low"}


def _setup_fallback(tmp_path):
    """Synthetic extras: flats building, parcel node, 25 m mutual node, plus no-override / one-use traps."""
    con = duckdb.connect()
    con.execute("LOAD spatial")
    pq, staged = tmp_path / "parquet", tmp_path / "staged"
    # uprns: 1,2,3 flats in building A (EPC flat evidence for 1); 4,5 in building B (no evidence);
    # 6 alone in parcel with one node; 7 and 8 share a node 15 m away (8 is closer -> only 8, one use);
    # 9: node 20 m away, street agrees (25 m tier); 10: node 20 m away, street differs; 11: strict node 3 m + a
    # building with flats tag containing 11 and 12 (strict must win for 11)
    u = {1: (100, 100), 2: (104, 100), 3: (108, 100), 4: (200, 100), 5: (204, 100), 6: (300, 100),
         7: (400, 100), 8: (430, 100), 9: (500, 100), 10: (600, 100), 11: (700, 100), 12: (704, 100)}  # fmt: skip
    vals = ",".join(f"({k}::BIGINT,{x}.0,{y}.0)" for k, (x, y) in u.items())
    _copy(con, f"SELECT * FROM (VALUES {vals}) t(uprn,x,y)", staged / "E1" / "open_uprn.parquet")
    _copy(con, f"SELECT uprn FROM (VALUES {','.join(f'({k}::BIGINT)' for k in u)}) t(uprn)",
          staged / "E1" / "onsud.parquet")  # fmt: skip
    _copy(con, "SELECT * FROM (VALUES (9::BIGINT,'Mill Road',NULL),(10,'Mill Road',NULL)) t(uprn,street_name,street_name_alt)",
          staged / "E1" / "streets.parquet")  # fmt: skip
    _copy(con, "SELECT 1::BIGINT uprn, 'Flat' property_type", staged / "E1" / "epc.parquet")
    _copy(con, "SELECT * FROM (VALUES (6::BIGINT,'P6',1),(11,'P11',1)) t(uprn,parcel_inspire_id,uprns_in_parcel)",
          staged / "E1" / "inspire_parcel.parquet")  # fmt: skip
    _copy(con, f"SELECT 'P6' parcel_inspire_id, ST_AsWKB(ST_GeomFromText('{_sq(290, 90, 20)}')) geom_wkb UNION ALL "
               f"SELECT 'P11', ST_AsWKB(ST_GeomFromText('{_sq(690, 90, 5)}'))",
          staged / "E1" / "inspire_polygons.parquet")  # fmt: skip
    _copy(con, "SELECT 1::BIGINT way_id, 'residential' highway, 'Mill Rd' name, NULL name_cy, NULL name_gd, "
               "0.0 xmin, 0.0 ymin, 1.0 xmax, 1.0 ymax, ST_AsWKB(ST_GeomFromText('LINESTRING(0 0,1 1)')) geom_wkb",
          pq / "osm" / "highways.parquet")  # fmt: skip
    # (type, id, number, street, unit, is_building, x, y, polygon)
    rows = [
        ("way", 201, "10", "Mill Road", None, True, 98, 98, _sq(98, 98, 14)),  # A: uprns 1-3, flat evidence
        ("way", 202, "20", "Mill Road", None, True, 198, 98, _sq(198, 98, 10)),  # B: uprns 4,5 no evidence
        ("node", 203, "30", "Mill Road", None, False, 309, 109, None),  # only node in parcel of 6
        ("node", 204, "40", "Mill Road", None, False, 415, 100, None),  # 15 m from 7, 15 m from 8 -> tie-ish
        ("node", 205, "50", "Mill Road", None, False, 520, 100, None),  # 20 m from 9, street agrees
        ("node", 206, "60", "Other Street", None, False, 620, 100, None),  # 20 m from 10, street differs
        ("node", 207, "70", "Mill Road", None, False, 701, 100, None),  # strict 10 m node for 11
        ("way", 208, "80", "Mill Road", "2", True, 698, 98, _sq(698, 98, 10)),  # flats tag, holds 11 and 12
    ]  # fmt: skip
    sel = " UNION ALL ".join(
        f"SELECT '{t}' osm_type, {i}::BIGINT osm_id, '{n}' housenumber, NULL housename, '{s}' street, "
        f"{'NULL' if un is None else repr(un)} unit, NULL flats, 'N1 1AA' postcode, NULL city, NULL ref_gb_uprn, "
        f"{b} is_building, {x}.0 x, {y}.0 y, {geom(p)} geom_wkb"
        for t, i, n, s, un, b, x, y, p in rows
    )
    _copy(con, sel, pq / "osm" / "addresses.parquet")
    return staged, tmp_path / "out", pq


def test_fallback_tiers(tmp_path):
    staged, out, pq = _setup_fallback(tmp_path)
    osm_for_lad("E1", staged, out, pq)
    con = duckdb.connect()
    rows = con.execute(f"SELECT * FROM read_parquet('{out}/lad=E1/streets_odbl.parquet') ORDER BY uprn").fetchall()
    cols = [d[0] for d in con.description]
    r = {x[0]: dict(zip(cols, x, strict=True)) for x in rows}
    # a. flats building with EPC flat evidence: building-level address for all three UPRNs
    for u in (1, 2, 3):
        assert r[u]["osm_addr_match_method"] == "inside_building_flats" and r[u]["osm_housenumber"] == "10"
        assert r[u]["building_level_address"] is True
        assert r[u]["osm_addr_confidence"] == CONFIDENCE["inside_building_flats"]
    # no flat evidence -> nothing
    assert r[4]["osm_addr_match_method"] is None and r[5]["osm_addr_match_method"] is None
    # b. one node in single-UPRN parcel
    assert (r[6]["osm_addr_match_method"], r[6]["osm_housenumber"]) == ("parcel_single_node", "30")
    assert r[6]["building_level_address"] is False
    # c. mutual nearest within 25 m needs agreeing street; node 204 is mutual-nearest to neither tie-free pair
    # (UPRN 7 is 15 m from it, UPRN 8 is 15 m away: tie -> rejected)
    assert r[7]["osm_addr_match_method"] is None and r[8]["osm_addr_match_method"] is None
    assert (r[9]["osm_addr_match_method"], r[9]["osm_housenumber"]) == ("nearest_node_25m_mutual", "50")
    assert r[9]["osm_addr_confidence"] == CONFIDENCE["nearest_node_25m_mutual"]
    assert r[10]["osm_addr_match_method"] is None  # street 'Other Street' != 'Mill Road'
    # no override: UPRN 11 keeps its strict 10 m match even though a flats-tagged building and a parcel hold it
    assert (r[11]["osm_addr_match_method"], r[11]["osm_housenumber"]) == ("nearest_node_10m", "70")
    assert r[11]["osm_addr_confidence"] == CONFIDENCE[r[11]["osm_addr_match_method"]]
    # UPRN 12 is in the flats building (tag evidence) -> building-level, number 80
    assert (r[12]["osm_addr_match_method"], r[12]["osm_housenumber"]) == ("inside_building_flats", "80")
    # one-use: every non-flats object serves at most one UPRN
    used = [(v["osm_addr_osm_type"], v["osm_addr_osm_id"]) for v in r.values()
            if v["osm_addr_match_method"] not in (None, "ref_gb_uprn", "inside_building_flats")]  # fmt: skip
    assert len(used) == len(set(used))


def test_osm_number_set():
    assert _osm_number_set("12a") == {"12"}
    assert _osm_number_set("10-14") == {"10", "11", "12", "13", "14"}
    assert _osm_number_set("1;3") == {"1", "3"}
    assert _osm_number_set(None) == set()


def test_single_counts_national_uprns_and_keeps_no_coord_uprns(tmp_path):
    staged, out, pq = _setup(tmp_path)
    con = duckdb.connect()
    con.execute("LOAD spatial")
    # national Open UPRN: this LAD's 10 UPRNs plus UPRN 99 (another LAD) inside UPRN 2's building polygon
    uprns = staged / "E1" / "open_uprn.parquet"
    _copy(con, f"SELECT uprn, x, y FROM read_parquet('{uprns}') UNION ALL SELECT 99::BIGINT, 1100.0, 1002.0",
          pq / "open_uprn" / "open_uprn.parquet")  # fmt: skip
    # UPRN 11 is in ONSUD but has no Open UPRN coordinates
    onsud = staged / "E1" / "onsud.parquet"
    old = tmp_path / "onsud_old.parquet"
    onsud.rename(old)
    _copy(con, f"SELECT uprn, lad FROM read_parquet('{old}') UNION ALL SELECT 11::BIGINT, 'E1'", onsud)
    osm_for_lad("E1", staged, out, pq)
    rows = con.execute(f"SELECT * FROM read_parquet('{out}/lad=E1/streets_odbl.parquet') ORDER BY uprn").fetchall()
    cols = [d[0] for d in con.description]
    r = {x[0]: dict(zip(cols, x, strict=True)) for x in rows}
    assert len(r) == 11
    assert r[2]["osm_addr_match_method"] is None  # building also holds a neighbouring-LAD UPRN
    assert r[11]["osm_street_name"] is None and r[11]["osm_addr_match_method"] is None
    assert r[11]["licence"] == "ODbL-1.0"


def test_outlier_uprn_does_not_change_results(tmp_path):
    """A UPRN with a wildly distant coordinate (huge LAD bbox) must not alter other UPRNs' matches."""
    staged, out, pq = _setup(tmp_path)
    con = duckdb.connect()
    con.execute("LOAD spatial")
    osm_for_lad("E1", staged, out, pq)
    f = out / "lad=E1" / "streets_odbl.parquet"
    base = con.execute(f"SELECT * FROM read_parquet('{f}') ORDER BY uprn").fetchall()
    uprns = staged / "E1" / "open_uprn.parquet"
    old = tmp_path / "ou_old.parquet"
    uprns.rename(old)
    _copy(con, f"SELECT uprn, x, y FROM read_parquet('{old}') UNION ALL SELECT 777::BIGINT, 540000.0, 260000.0", uprns)
    onsud = staged / "E1" / "onsud.parquet"
    old2 = tmp_path / "onsud_old2.parquet"
    onsud.rename(old2)
    _copy(con, f"SELECT uprn, lad FROM read_parquet('{old2}') UNION ALL SELECT 777::BIGINT, 'E1'", onsud)
    osm_for_lad("E1", staged, out, pq)
    got = con.execute(f"SELECT * FROM read_parquet('{f}') ORDER BY uprn").fetchall()
    assert [r for r in got if r[0] != 777] == base
    assert len(got) == len(base) + 1
