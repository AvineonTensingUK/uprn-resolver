import duckdb
import pytest

from uprn_resolver.resolve import create_views, lookup, resolve_all, resolve_lad


def _copy(con, sql, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    con.execute(f"COPY ({sql}) TO '{path}' (FORMAT parquet)")


UPRN = """SELECT * FROM (VALUES
 (1::BIGINT, 100.0, 100.0, 'B1', 1, 'P1', 1, 'S1', 'Alpha Rd', 'Town', 10::BIGINT, 'E01', 'Detached', 'House', false),
 (2, 110, 110, 'B2', 4, 'P2', 4, 'S2', 'Alpha Rd', 'Town', 10, 'E01', NULL, NULL, false),
 (3, 120, 120, 'B3', 20, 'P3', 2, 'S3', NULL, NULL, 11, 'E01', NULL, NULL, false),
 (4, 130, 130, 'B3', 20, NULL, NULL, 'S3', NULL, NULL, 11, 'E01', NULL, NULL, true),
 (5, 140, 140, NULL, NULL, 'P4', 8, NULL, 'Beta St', 'Town', 12, 'E01', NULL, NULL, false),
 (6, 150, 150, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, 'E01', NULL, NULL, false)
) t(uprn, x, y, building_id, uprns_in_building, parcel_inspire_id, uprns_in_parcel, junk, street_name, locality,
    usrn, lsoa, epc_property_type, epc_built_form, methods_disagree)"""


def _setup(tmp_path, *, optional=True):
    con = duckdb.connect()
    out = tmp_path / "out"
    d = out / "lad=E1"
    _copy(con, UPRN.replace("junk", "msoa"), d / "uprn.parquet")
    if not optional:
        return out
    _copy(
        con,
        """SELECT * FROM (VALUES
        (1::BIGINT, 10::BIGINT, 'ok', NULL, 'AB1 1AA', 'ok'),
        (3, 11, 'far', 'Gamma Way', 'AB1 1AB', 'not_in_codepoint'),
        (6, 12, 'ok', NULL, 'AB1 1AC', 'ok')) t(uprn, usrn, usrn_link_flag, street_name_fallback,
        postcode_onsud, postcode_flag)""",
        d / "provenance.parquet",
    )
    _copy(
        con,
        """SELECT 1::BIGINT uprn, 'Flat' property_type, 'Mid-Terrace' built_form, 'L1' lmk_key,
        'domestic' certificate_type, DATE '2020-05-01' lodgement_date, 3::BIGINT epc_count""",
        d / "epc.parquet",
    )
    _copy(
        con,
        """SELECT * FROM (VALUES
        (1::BIGINT, 'Shop A', 'cafe', 'high', 0.9, 1.0, 'p1'),
        (2, 'Shop B', 'bar', 'low', 0.9, 1.0, 'p2'),
        (3, 'Shop C', 'bar', 'medium', 0.5, 5.0, 'p3'),
        (3, 'Shop D', 'bar', 'high', 0.7, 5.0, 'p4')) t(uprn, name, category, match_confidence,
        overture_confidence, match_distance_m, poi_id)""",
        d / "poi.parquet",
    )
    _copy(
        con,
        """SELECT * FROM (VALUES
        (1::BIGINT, '1 Alpha Road, London', 'epc_domestic', 'EPC-restricted', 'Alpha Rd', 'Town', 'AB1 1AA'),
        (2, NULL, 'none', 'open-partial', 'Alpha Rd', 'Town', 'AB1 1AA'),
        (3, NULL, 'none', 'open-partial', NULL, NULL, 'AB1 1AB'),
        (4, NULL, 'none', 'open-partial', NULL, NULL, NULL),
        (5, NULL, 'none', 'open-partial', 'Beta St', 'Town', 'AB1 1AD'),
        (6, NULL, 'none', 'open-partial', NULL, NULL, NULL)) t(uprn, single_line_address, address_source, licence,
        street_name_open, locality_open, postcode_onsud)""",
        d / "address_local.parquet",
    )
    _copy(
        con,
        """SELECT * FROM (VALUES
        (1::BIGINT, '7', NULL, 'Alpha Rd', 'ref_gb_uprn', 'high', false, NULL),
        (2, '9', NULL, 'Alpha Rd', 'ref_gb_uprn', 'high', false, 'Flat 2'),
        (3, NULL, 'The Hall', NULL, 'nearest_node_10m', 'low', false, NULL),
        (4, '5', NULL, 'Delta Rd', 'inside_building_single', 'medium', true, 'Flat 9'),
        (5, '3', NULL, NULL, 'nearest_node_25m_mutual', NULL, false, NULL),
        (6, NULL, NULL, 'X', 'inside_building_single', 'medium', false, NULL)) t(uprn, osm_housenumber,
        osm_housename, osm_addr_street, osm_addr_match_method, osm_addr_confidence, building_level_address,
        osm_unit)""",
        d / "streets_odbl.parquet",
    )
    return out


def _rows(out, name, cols="*"):
    con = duckdb.connect()
    cur = con.execute(f"SELECT {cols} FROM read_parquet('{out}/lad=E1/{name}.parquet') ORDER BY uprn")
    names = [c[0] for c in cur.description]
    return {r[0]: dict(zip(names, r, strict=True)) for r in cur.fetchall()}


def test_outline_choice_and_confidence(tmp_path):
    out = _setup(tmp_path)
    resolve_lad("E1", out)
    r = _rows(out, "resolved_open", "* EXCLUDE (geometry)")
    assert (r[1]["best_outline_source"], r[1]["best_outline_id"], r[1]["outline_confidence"]) == (
        "openmap_building",
        "B1",
        "property",
    )
    assert (r[2]["best_outline_source"], r[2]["outline_confidence"], r[2]["outline_uprn_count"]) == (
        "openmap_building",
        "small_group",
        4,
    )
    # parcel strictly finer than the building: parcel id, parcel count
    assert (
        r[3]["best_outline_source"],
        r[3]["best_outline_id"],
        r[3]["outline_uprn_count"],
        r[3]["outline_confidence"],
    ) == ("inspire_parcel", "P3", 2, "small_group")
    # no parcel: shared building, block
    assert (r[4]["best_outline_source"], r[4]["outline_confidence"]) == ("openmap_building", "block")
    # no building, parcel only
    assert (r[5]["best_outline_source"], r[5]["outline_confidence"]) == ("inspire_parcel", "block")
    assert (r[6]["best_outline_source"], r[6]["outline_confidence"], r[6]["best_outline_id"]) == ("none", "none", None)


def test_street_epc_poi_and_quality(tmp_path):
    out = _setup(tmp_path)
    resolve_lad("E1", out)
    r = _rows(out, "resolved_open", "* EXCLUDE (geometry)")
    assert r[1]["street_source"] == "os_open_names" and r[1]["quality"] == "ok" and r[1]["quality_score"] == 100
    assert r[3]["street_name"] == "Gamma Way" and r[3]["street_source"] == "os_open_roads"
    assert "street far from USRN" in r[3]["quality"] and "Open Roads" in r[3]["quality"]
    assert "no street name" in r[6]["quality"] and "no outline" in r[6]["quality"]
    assert "building match methods disagree" in r[4]["quality"]
    assert r[1]["property_type_epc"] == "Flat" and r[1]["built_form_epc"] == "Mid-Terrace"
    assert r[1]["epc_lmk_key"] == "L1" and r[1]["epc_certificate_type"] == "domestic"
    assert str(r[1]["epc_lodgement_date"]) == "2020-05-01" and r[1]["epc_count"] == 3
    assert r[2]["epc_lmk_key"] is None
    assert r[1]["lat"] is not None and 49 < r[1]["lat"] < 61
    # POI: low excluded; high beats medium
    assert r[1]["poi_name"] == "Shop A" and r[2]["poi_name"] is None and r[3]["poi_name"] == "Shop D"
    assert "CDLA" in r[1]["poi_licence"] and r[2]["poi_licence"] is None


def test_postcode_flag_and_no_address_in_open(tmp_path):
    out = _setup(tmp_path)
    resolve_lad("E1", out)
    cols = set(_rows(out, "resolved_open")[1])
    assert "postcode" not in cols and not any("address" in c for c in cols)
    resolve_lad("E1", out, include_postcode=True)
    r = _rows(out, "resolved_open")
    assert r[1]["postcode"] == "AB1 1AA"
    assert (out / "lad=E1" / "resolved_open.LICENCE.txt").exists()
    assert "DO NOT PUBLISH" in (out / "lad=E1" / "resolved_local.LICENCE.txt").read_text()


def test_address_precedence_and_licence(tmp_path):
    out = _setup(tmp_path)
    resolve_lad("E1", out)
    r = _rows(out, "resolved_local", "* EXCLUDE (geometry)")
    # EPC beats OSM
    assert (r[1]["address_source"], r[1]["address_licence"], r[1]["single_line_address"]) == (
        "epc",
        "EPC-restricted",
        "1 Alpha Road, London",
    )
    # OSM beats open partial
    assert (r[2]["address_source"], r[2]["address_licence"]) == ("osm_high", "ODbL-1.0")
    assert r[2]["address_source"] == "osm_high"
    assert r[2]["single_line_address"] == "Flat 2, 9 Alpha Rd, Town, AB1 1AA"
    assert "OSM (high confidence)" in r[2]["quality"]
    # low confidence is never chosen: open partial + separate candidate fields
    assert r[3]["address_source"] != "osm_low" and r[3]["address_licence"] != "ODbL-1.0"
    assert r[3]["osm_address_candidate"].startswith("The Hall")
    assert r[3]["osm_address_candidate_confidence"] == "low"
    assert r[2]["osm_address_candidate"] is None
    # medium, building-level: chosen, but the flat/unit is never presented
    assert r[4]["address_source"] == "osm_medium" and r[4]["address_building_level"] is True
    assert r[4]["single_line_address"] == "5 Delta Rd, AB1 1AB" or r[4]["single_line_address"].startswith("5 Delta Rd")
    assert "Flat" not in r[4]["single_line_address"] and "building-level" in r[4]["quality"]
    # unaccepted OSM method ignored -> open partial
    assert (r[5]["address_source"], r[5]["address_licence"], r[5]["single_line_address"]) == (
        "open_partial",
        "OGL",
        "Beta St, Town, AB1 1AD",
    )
    # OSM with only a street but no number/name is not an address; no open data at all -> NULL
    assert (r[6]["address_source"], r[6]["address_licence"]) == ("open_partial", "OGL")
    # local always has postcode
    assert r[1]["postcode"] == "AB1 1AA"
    assert set(_rows(out, "resolved_open")[1]) <= set(r[1]) | {"postcode", "geometry"}


def test_missing_optional_inputs(tmp_path):
    out = _setup(tmp_path, optional=False)
    stats = resolve_lad("E1", out)
    assert "provenance" in stats["missing_inputs"] and "streets_odbl" in stats["missing_inputs"]
    r = _rows(out, "resolved_open", "* EXCLUDE (geometry)")
    assert r[1]["usrn_link_flag"] is None and r[1]["poi_name"] is None
    assert "no provenance" in r[1]["quality"] and "no poi" in r[1]["quality"]
    # epc columns fall back to uprn.parquet
    assert r[1]["property_type_epc"] == "Detached" and r[1]["built_form_epc"] == "House"
    assert r[1]["epc_lmk_key"] is None
    assert r[1]["street_name"] == "Alpha Rd" and r[1]["usrn"] == 10
    loc = _rows(out, "resolved_local", "* EXCLUDE (geometry)")
    assert loc[1]["address_source"] == "open_partial"
    assert loc[6]["address_source"] is None


def test_geoparquet_metadata(tmp_path):
    out = _setup(tmp_path)
    resolve_lad("E1", out)
    con = duckdb.connect()
    kv = dict(
        con.execute(f"SELECT key, value FROM parquet_kv_metadata('{out}/lad=E1/resolved_open.parquet')").fetchall()
    )
    assert b"27700" in kv[b"geo"] if isinstance(next(iter(kv)), bytes) else "27700" in kv["geo"]


def test_resolve_all_resumable_views_and_lookup(tmp_path):
    out = _setup(tmp_path)
    res = resolve_all(out)
    assert res["done"] == ["E1"] and not res["failed"]
    assert resolve_all(out)["skipped"] == ["E1"]
    con = duckdb.connect()
    create_views(con, out)
    assert con.execute("SELECT count(*) FROM resolved WHERE uprn = 1").fetchall()[0][0] == 1
    assert con.execute("SELECT count(*) FROM resolved_local").fetchall()[0][0] == 6
    o = lookup(1, out)
    assert o["best_outline_id"] == "B1" and "single_line_address" not in o
    loc = lookup(1, out, local=True)
    assert loc["single_line_address"] == "1 Alpha Road, London"
    assert lookup(999, out) is None
    with pytest.raises(FileNotFoundError):
        resolve_lad("NOPE", out)


def _outline_fixture(tmp_path, ms_rows=None, osm_rows=None):
    con = duckdb.connect()
    out = tmp_path / "out"
    d = out / "lad=E1"
    _copy(
        con,
        """SELECT * FROM (VALUES
     (1::BIGINT, 1.0, 1.0, 'B1', 10, 'P1', 6),
     (2, 2.0, 2.0, 'B2', 10, 'P2', 10),
     (3, 3.0, 3.0, 'B3', 10, 'P3', 3),
     (4, 4.0, 4.0, NULL, NULL, 'P4', 8),
     (5, 5.0, 5.0, 'B5', 3, NULL, NULL),
     (6, 6.0, 6.0, 'B6', 10, 'P6', 10),
     (7, 7.0, 7.0, NULL, NULL, NULL, NULL)
    ) t(uprn, x, y, building_id, uprns_in_building, parcel_inspire_id, uprns_in_parcel)""",
        d / "uprn.parquet",
    )
    if ms_rows:
        _copy(
            con,
            f"""SELECT * FROM (VALUES {ms_rows}) t(uprn, ms_building_id, uprns_in_ms_building, match_method,
            area_ratio_ms_to_ogl)""",
            d / "outline_ms.parquet",
        )
    if osm_rows:
        _copy(
            con,
            f"""SELECT * FROM (VALUES {osm_rows}) t(uprn, osm_building_id, uprns_in_osm_building,
            osm_match_method, osm_ogl_area_ratio)""",
            d / "outline_odbl.parquet",
        )
    return out


MS = """(1::BIGINT, 'M1', 1, 'toid_point_in_polygon', 0.5),
 (2, 'M2', 3, 'toid_point_in_polygon', 0.9),
 (3, 'M3', 3, 'toid_point_in_polygon', 1.0),
 (4, 'M4', 2, 'nearest_within_5m', NULL),
 (5, 'M5', 2, 'nearest_within_5m', 0.5),
 (6, 'M6', 2, 'nearest_within_5m', 0.9),
 (7, 'M7', 1, 'uprn_point_in_polygon', NULL)"""
OSM = """(1::BIGINT, 'W1', 1, 'toid_point_in', 0.5),
 (2, 'W2', 2, 'uprn_point_in', 0.9),
 (3, 'W3', 1, 'toid_point_in', 1.0),
 (6, 'W6', 1, 'nearest_5m', 0.5),
 (7, 'W7', 1, 'nearest_5m', NULL)"""


def test_ms_osm_choice_licences_and_guard(tmp_path):
    out = _outline_fixture(tmp_path, MS, OSM)
    stats = resolve_lad("E1", out)
    o = _rows(out, "resolved_open", "* EXCLUDE (geometry)")
    loc = _rows(out, "resolved_local", "* EXCLUDE (geometry)")
    pick = lambda r, u, sfx="": (r[u]["best_outline_source" + sfx], r[u]["best_outline_id" + sfx])  # noqa: E731
    # 1: MS (1) beats OpenMap (10) and parcel (6); local: OSM ties MS at 1 -> OSM preferred
    assert pick(o, 1) == ("ms_building", "M1") and o[1]["best_outline_licence"] == "CDLA-Permissive-2.0"
    assert o[1]["outline_confidence"] == "property" and "outline from Microsoft footprints" in o[1]["quality"]
    assert pick(loc, 1, "_local") == ("osm_building", "W1")
    assert loc[1]["best_outline_licence_local"] == "ODbL-1.0"
    assert "outline from OSM (local only)" in loc[1]["quality"]
    assert pick(loc, 1) == ("ms_building", "M1")  # open-lane columns kept in local
    # 2: parcel 10, building 10, MS 3 -> MS open; OSM 2 -> OSM local small_group
    assert pick(o, 2) == ("ms_building", "M2") and o[2]["outline_confidence"] == "small_group"
    assert pick(loc, 2, "_local") == ("osm_building", "W2")
    # 3: parcel 3, MS 3 (tie): MS beats parcel; OSM 1 local
    assert pick(o, 3) == ("ms_building", "M3")
    # 4: no building: nearest MS allowed (2 < parcel 8); nearest OSM missing
    assert pick(o, 4) == ("ms_building", "M4")
    # 5: OpenMap 3 contains point; nearest MS (2, ratio .5) strictly fewer + ratio<0.8 -> allowed
    assert pick(o, 5) == ("ms_building", "M5")
    # 6: nearest MS ratio .9 -> ignored; nearest OSM ratio .5 and fewer -> allowed locally only
    assert pick(o, 6) == ("openmap_building", "B6")
    assert pick(loc, 6, "_local") == ("osm_building", "W6")
    # 7: no OpenMap, so nearest OSM accepted locally; MS point-in accepted
    assert pick(o, 7) == ("ms_building", "M7") and pick(loc, 7, "_local") == ("osm_building", "W7")
    assert stats["best_outline_source"]["ms_building"] == 6
    assert stats["best_outline_source_local"]["osm_building"] == 5
    assert "outline_confidence_local" in stats


def test_no_osm_in_open_and_ties(tmp_path):
    out = _outline_fixture(tmp_path, MS, OSM)
    resolve_lad("E1", out)
    o = _rows(out, "resolved_open", "* EXCLUDE (geometry)")
    assert not any(c.endswith("_local") for c in o[1])
    assert {r["best_outline_source"] for r in o.values()} <= {
        "openmap_building",
        "ms_building",
        "inspire_parcel",
        "ros_parcel",
        "none",
    }
    assert not any("OSM" in r["quality"] or "ODbL" in str(r["best_outline_licence"]) for r in o.values())
    # tie OpenMap vs MS vs parcel -> OpenMap
    out2 = _outline_fixture(
        tmp_path / "b",
        "(5::BIGINT, 'M5', 3, 'toid_point_in_polygon', 1.0)",
        "(5::BIGINT, 'W5', 3, 'toid_point_in', 1.0)",
    )
    resolve_lad("E1", out2)
    assert _rows(out2, "resolved_open")[5]["best_outline_source"] == "openmap_building"
    assert _rows(out2, "resolved_local")[5]["best_outline_source_local"] == "openmap_building"


def test_missing_outline_inputs_fall_back(tmp_path):
    out = _outline_fixture(tmp_path)
    stats = resolve_lad("E1", out)
    assert "outline_ms" in stats["missing_inputs"] and "outline_odbl" in stats["missing_inputs"]
    o = _rows(out, "resolved_open", "* EXCLUDE (geometry)")
    loc = _rows(out, "resolved_local", "* EXCLUDE (geometry)")
    assert o[1]["best_outline_source"] == "inspire_parcel" and o[1]["best_outline_licence"] == "OGL-3.0"
    assert loc[1]["best_outline_source_local"] == "inspire_parcel"
    assert o[7]["best_outline_source"] == "none" and o[7]["best_outline_licence"] is None
    # only MS present: local lane still works, without OSM
    out3 = _outline_fixture(tmp_path / "c", MS)
    resolve_lad("E1", out3)
    assert _rows(out3, "resolved_local")[1]["best_outline_source_local"] == "ms_building"


# --------------------------------------------------------------------------- review fixes
def test_crash_mid_resolve_leaves_no_marker(tmp_path, monkeypatch):
    import uprn_resolver.resolve as R

    out = _setup(tmp_path)
    resolve_lad("E1", out)
    d = out / "lad=E1"
    assert (d / "resolved.stats.json").exists()
    real = R.write_text_atomic

    def dying(path, text):
        if path.name == "resolved_local.LICENCE.txt":
            raise MemoryError("OOM")  # both parquet products were already rewritten
        return real(path, text)

    monkeypatch.setattr(R, "write_text_atomic", dying)
    res = resolve_all(out, skip_existing=False)
    assert "E1" in res["failed"]
    assert not (d / "resolved.stats.json").exists()
    monkeypatch.setattr(R, "write_text_atomic", real)
    assert resolve_all(out)["done"] == ["E1"]  # default run must not skip the half-written LAD
    assert not list(d.glob("*.tmp"))


def test_resolve_cleans_stray_tmp(tmp_path):
    out = _setup(tmp_path)
    d = out / "lad=E1"
    (d / "resolved_open.parquet.tmp").write_text("junk")
    resolve_lad("E1", out)
    assert not (d / "resolved_open.parquet.tmp").exists()


def test_resolve_all_reruns_when_inputs_newer(tmp_path):
    import os
    import time

    out = _setup(tmp_path)
    assert resolve_all(out)["done"] == ["E1"]
    assert resolve_all(out)["skipped"] == ["E1"]
    old = time.time() - 1000
    for n in ("resolved_open.parquet", "resolved_local.parquet", "resolved.stats.json"):
        os.utime(out / "lad=E1" / n, (old, old))  # uprn.parquet is now newer than the resolved outputs
    assert resolve_all(out)["done"] == ["E1"]
    assert resolve_all(out)["skipped"] == ["E1"]
    t = time.time() + 100
    os.utime(out / "lad=E1" / "poi.parquet", (t, t))  # an optional input rebuilt
    assert resolve_all(out)["done"] == ["E1"]


def test_national_building_counts_used_for_openmap_outline(tmp_path):
    import os

    out = _setup(tmp_path)
    nat = out / "national"
    nat.mkdir()
    duckdb.connect().execute(
        f"COPY (SELECT * FROM (VALUES ('B2', 9), ('B3', 30), ('B1', 1)) v(building_id, uprns_in_building_national)) "
        f"TO '{nat / 'building_uprn_counts.parquet'}' (FORMAT PARQUET)"
    )
    s = resolve_lad("E1", out)
    r = _rows(out, "resolved_open", "* EXCLUDE (geometry)")
    assert s["building_counts"] == "national"
    # uprn 4: no parcel, building B3 had a LAD-local count of 20 and spans LADs (30 nationally)
    assert (r[4]["best_outline_source"], r[4]["outline_uprn_count"], r[4]["outline_confidence"]) == (
        "openmap_building",
        30,
        "block",
    )
    # uprn 2: B2 is 9 nationally (was 4 locally) so the 4-UPRN parcel P2 is now strictly finer
    assert (r[2]["best_outline_source"], r[2]["outline_uprn_count"]) == ("inspire_parcel", 4)
    assert r[1]["outline_uprn_count"] == 1
    # counts older than uprn.parquet are not trusted: fall back to LAD-local
    os.utime(nat / "building_uprn_counts.parquet", (1, 1))
    s = resolve_lad("E1", out)
    r = _rows(out, "resolved_open", "* EXCLUDE (geometry)")
    assert s["building_counts"] == "lad_local" and r[4]["outline_uprn_count"] == 20


def test_epc_dedupe_is_deterministic(tmp_path):
    out = _setup(tmp_path, optional=False)
    d = out / "lad=E1"
    con = duckdb.connect()
    for order in ("ASC", "DESC"):
        _copy(
            con,
            f"""SELECT * FROM (VALUES (1::BIGINT, 'Flat', 'Old', DATE '2015-01-01', 'a'),
                (1, 'House', 'New', DATE '2020-01-01', 'b')) v(uprn, property_type, built_form, lodgement_date, lmk_key)
                ORDER BY lmk_key {order}""",
            d / "epc.parquet",
        )
        resolve_lad("E1", out)
        r = _rows(out, "resolved_open", "uprn, property_type_epc, built_form_epc")
        assert (r[1]["property_type_epc"], r[1]["built_form_epc"]) == ("House", "New"), order


def _add_ppd(out):
    con = duckdb.connect()
    d = out / "lad=E1"
    _copy(
        con,
        """SELECT * FROM (VALUES
        (2::BIGINT, '7 Alpha Rd, Town, AB1 1AA', 'ppd', 'PPD-restricted', 'Alpha Rd', 'Town', 'AB1 1AA'),
        (3, '3 Gamma Way, AB1 1AB', 'ppd', 'PPD-restricted', NULL, NULL, 'AB1 1AB')) t(uprn, single_line_address,
        address_source, licence, street_name_open, locality_open, postcode_onsud)""",
        d / "address_local_ppd.parquet",
    )
    # replace rows 2 and 3 of address_local with PPD rows, keep the rest
    _copy(
        con,
        f"""SELECT * FROM read_parquet('{d / "address_local.parquet"}') WHERE uprn NOT IN (2, 3)
        UNION ALL BY NAME SELECT * FROM read_parquet('{d / "address_local_ppd.parquet"}')""",
        d / "address_local_new.parquet",
    )
    (d / "address_local_ppd.parquet").unlink()
    (d / "address_local_new.parquet").replace(d / "address_local.parquet")
    _copy(
        con,
        """SELECT * FROM (VALUES
        (1::BIGINT, 3::BIGINT, 250000::BIGINT, DATE '2021-03-01', DATE '2005-06-01'),
        (2, 1, 100000, DATE '2019-01-01', DATE '2019-01-01')) t(uprn, sales_count, last_price, last_sale_date,
        first_sale_date)""",
        d / "ppd_local_summary.parquet",
    )


def test_ppd_address_precedence_and_sales(tmp_path):
    out = _setup(tmp_path)
    _add_ppd(out)
    resolve_lad("E1", out)
    r = _rows(out, "resolved_local", "* EXCLUDE (geometry)")
    # EPC > PPD > OSM
    assert r[1]["address_source"] == "epc"
    assert (r[2]["address_source"], r[2]["address_licence"]) == ("ppd", "PPD-restricted")
    assert r[2]["single_line_address"] == "7 Alpha Rd, Town, AB1 1AA"
    assert r[2]["address_osm_method"] is None and "HMLR Price Paid" in r[2]["quality"]
    assert r[3]["address_source"] == "ppd" and r[3]["osm_address_candidate"] is None
    assert r[4]["address_source"] == "osm_medium"  # no PPD row: OSM still applies
    # sales summary: local only, exact columns
    assert (r[1]["ppd_sales_count"], r[1]["ppd_last_price"]) == (3, 250000)
    assert str(r[1]["ppd_last_sale_date"]) == "2021-03-01" and str(r[1]["ppd_first_sale_date"]) == "2005-06-01"
    assert r[5]["ppd_sales_count"] is None
    o = _rows(out, "resolved_open", "* EXCLUDE (geometry)")
    assert not [c for c in o[1] if c.startswith("ppd_")]
    assert "single_line_address" not in o[1]


def test_no_ppd_files_resolve(tmp_path):
    out = _setup(tmp_path)  # fixture has no ppd_local_summary (e.g. Scotland)
    stats = resolve_lad("E1", out)
    assert "ppd_local_summary" in stats["missing_inputs"]
    r = _rows(out, "resolved_local", "* EXCLUDE (geometry)")
    assert r[1]["ppd_sales_count"] is None and r[1]["ppd_last_price"] is None
    assert r[1]["address_source"] == "epc" and r[2]["address_source"] == "osm_high"
