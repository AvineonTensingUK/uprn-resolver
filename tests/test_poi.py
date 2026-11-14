import duckdb

from uprn_resolver.poi import PLACES_REL, assign_places_lad, poi_all, poi_for_lad


def _sq(x, y, s):
    return f"POLYGON(({x} {y},{x + s} {y},{x + s} {y + s},{x} {y + s},{x} {y}))"


def _copy(con, sql, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    con.execute(f"COPY ({sql}) TO '{path}' (FORMAT parquet)")


def _setup(tmp_path):
    con = duckdb.connect()
    con.execute("LOAD spatial")
    pq, staged, out = tmp_path / "parquet", tmp_path / "staged", tmp_path / "out"
    # UPRNs: 1,2 in building B1 (2 is non-domestic); 3 alone in building B2; 4,5 in parcel P1 (no building);
    # 6 loose
    _copy(
        con,
        "SELECT * FROM (VALUES (1::BIGINT,5.0,5.0,'B1'),(2,8,8,'B1'),(3,105,105,'B2'),(4,205,205,NULL),"
        "(5,208,208,NULL),(6,400,400,NULL)) t(uprn,x,y,building_id)",
        out / "lad=E1" / "uprn.parquet",
    )
    _copy(
        con,
        f"SELECT 'B1' building_id, ST_AsWKB(ST_GeomFromText('{_sq(0, 0, 20)}')) geom_wkb UNION ALL "
        f"SELECT 'B2', ST_AsWKB(ST_GeomFromText('{_sq(100, 100, 10)}'))",
        out / "lad=E1" / "buildings.parquet",
    )
    _copy(
        con,
        "SELECT * FROM (VALUES (1::BIGINT,'domestic'),(2,'non-domestic'),(3,'domestic')) t(uprn,certificate_type)",
        staged / "E1" / "epc.parquet",
    )
    _copy(
        con,
        "SELECT * FROM (VALUES (4::BIGINT,'P1'),(5,'P1')) t(uprn,parcel_inspire_id)",
        staged / "E1" / "inspire_parcel.parquet",
    )
    _copy(
        con,
        f"SELECT 'P1' parcel_inspire_id, ST_AsWKB(ST_GeomFromText('{_sq(200, 200, 30)}')) geom_wkb",
        staged / "E1" / "inspire_polygons.parquet",
    )
    _copy(
        con,
        "SELECT *, NULL::VARCHAR AS taxonomy_primary FROM (VALUES "
        "('a','Cafe',  'cafe',  NULL,'open',0.9,10.0,10.0),"  # in B1 -> non-domestic uprn 2
        "('b','Shop',  'retail','Brand','open',0.8,106.0,106.0),"  # in B2 -> uprn 3, unique
        "('c','Office','office',NULL,'open',0.7,206.0,206.0),"  # in P1, 2 candidates
        "('d','Kiosk', 'retail',NULL,'open',0.5,402.0,400.0),"  # nearest 30m -> 6
        "('e','Far',   'retail',NULL,'open',0.5,300.0,300.0),"  # none
        "('f','Outside','retail',NULL,'open',0.5,99999.0,99999.0)"  # outside bbox
        ") t(poi_id,name,basic_category,brand_name,operating_status,confidence,x,y)",
        pq / PLACES_REL,
    )
    # national UPRN tables for the LAD assignment (UPRN 3 belongs to another LAD)
    _copy(
        con,
        "SELECT * FROM (VALUES (1::BIGINT,5.0,5.0),(2,8,8),(3,105,105),(4,205,205),(5,208,208),(6,400,400))"
        " t(uprn,x,y)",
        pq / "open_uprn" / "open_uprn.parquet",
    )
    _copy(
        con,
        "SELECT * FROM (VALUES (1::BIGINT,'E1'),(2,'E1'),(3,'E2'),(4,'E1'),(5,'E1'),(6,'E1')) t(uprn,lad)",
        pq / "onsud" / "onsud.parquet",
    )
    assert assign_places_lad(pq)["assigned"] == 4
    return pq, staged, out


def _rows(out):
    con = duckdb.connect()
    cols = "poi_id, uprn, match_method, match_confidence, candidates, match_distance_m"
    return {r[0]: r[1:] for r in con.execute(f"SELECT {cols} FROM '{out}/lad=E1/poi.parquet'").fetchall()}


def test_poi_for_lad(tmp_path):
    pq, staged, out = _setup(tmp_path)
    stats = poi_for_lad("E1", staged, out, pq)
    rows = _rows(out)
    assert set(rows) == {"a", "c", "d"}  # b belongs to E2, e/f have no UPRN within 100 m
    assert rows["a"][:4] == (2, "same_building", "high", 2)  # non-domestic uniquely chosen
    assert rows["c"][1] == "same_parcel" and rows["c"][2] == "medium" and rows["c"][3] == 2
    assert rows["c"][0] == 4  # nearest of 4/5
    assert rows["d"][:2] == (6, "nearest_30m") and rows["d"][2] == "high"
    assert stats["pois"] == 3 and stats["nondomestic_uprns_named"] == 1
    assert (out / "lad=E1" / "poi.LICENCE.txt").read_text().startswith("Overture Maps Places")


def test_poi_without_optional_inputs(tmp_path):
    pq, staged, out = _setup(tmp_path)
    for f in ("epc", "inspire_parcel", "inspire_polygons"):
        (staged / "E1" / f"{f}.parquet").unlink()
    poi_for_lad("E1", staged, out, pq)
    rows = _rows(out)
    assert rows["a"][1] == "same_building" and rows["a"][0] == 2  # nearest of two (no EPC preference)
    assert rows["c"][1] == "nearest_30m"


def test_poi_all_resumable(tmp_path):
    pq, staged, out = _setup(tmp_path)
    r1 = poi_all(staged, out, pq)
    assert r1["done"] == ["E1"]
    r2 = poi_all(staged, out, pq)
    assert r2["skipped"] == ["E1"] and not r2["done"]
    assert poi_all(staged, out, pq, lads=["E9"])["failed"]
