import gzip
import json

import duckdb
import pytest

from uprn_resolver import msbuildings as ms


def _sq(lon, lat, d=0.0003):
    return [[lon, lat], [lon + d, lat], [lon + d, lat + d], [lon, lat + d], [lon, lat]]


def _write_tile(path, polys, conf=-1.0):
    with gzip.open(path, "wt") as f:
        for ring in polys:
            f.write(
                json.dumps(
                    {
                        "type": "Feature",
                        "properties": {"height": 3.0, "confidence": conf},
                        "geometry": {"type": "Polygon", "coordinates": [ring]},
                    }
                )
                + "\n"
            )


def test_quadkey_bounds():
    b = ms.quadkey_bounds("031313131")
    assert b[0] < -0.1 < b[2] and b[1] < 51.5 < b[3]
    assert ms.quadkey_bounds("0")[0] == -180


def test_parse_size_and_uk_tiles(tmp_path):
    assert ms.parse_size("2B") == 2 and ms.parse_size("1.5MB") == 1.5e6
    links = tmp_path / "l.csv"
    links.write_text(
        "Location,QuadKey,Url,Size,UploadDate\n"
        "UnitedKingdom,031313131,http://x/a,113MB,2026-08-13\n"
        "UnitedKingdom,031310131,http://x/b,18.4MB,2026-08-13\n"
        "Ireland,031310101,http://x/c,402B,2026-08-13\n"
    )
    tiles = ms.uk_tiles(links)
    assert [t["quadkey"] for t in tiles] == ["031310131", "031313131"]
    assert tiles[0]["spans_ni"] and not tiles[1]["spans_ni"]


def test_convert_tile_gb_and_ni_split(tmp_path):
    raw = tmp_path / "t.csv.gz"
    # one GB polygon (Islington-ish), one NI polygon (Belfast), one empty-ish bad one is dropped
    _write_tile(raw, [_sq(-0.10, 51.54), _sq(-5.93, 54.60)])
    pq = tmp_path / "parquet"
    pq.mkdir()
    r = ms.convert_tile(raw, "031310131", pq, spans_ni=True, size_bytes=1, url="http://x")
    assert r["gb"] == 1 and r["ni"] == 1 and r["dropped"] == 0
    con = duckdb.connect()
    con.execute("LOAD spatial")
    gb = con.execute(
        f"SELECT ms_id, area_m2, bbox.xmin FROM read_parquet('{pq}/msbuildings/gb/031310131.parquet')"
    ).fetchone()
    assert len(gb[0]) == 16 and 300 < gb[1] < 1500 and 500000 < gb[2] < 560000
    ni = con.execute(f"SELECT bbox.xmin FROM read_parquet('{pq}/msbuildings/ni/031310131.parquet')").fetchone()
    assert 300000 < ni[0] < 400000
    cols = [
        d[0] for d in con.execute(f"SELECT * FROM read_parquet('{pq}/msbuildings/gb/031310131.parquet')").description
    ]
    assert "confidence" not in cols and "height" not in cols
    m = json.loads((pq / "manifest.json").read_text())["converted"]
    e = m["msbuildings/gb/031310131.parquet"]
    assert e["licence"] == "CDLA-Permissive-2.0" and e["rows"] == 1 and "Microsoft" in e["attribution"]
    assert "msbuildings/ni/031310131.parquet" in m
    assert ms.tile_done("031310131", pq) and not ms.tile_done("031313131", pq)


def test_convert_tile_keeps_confidence_when_real(tmp_path):
    raw = tmp_path / "t.csv.gz"
    _write_tile(raw, [_sq(-0.10, 51.54)], conf=0.8)
    pq = tmp_path / "parquet"
    pq.mkdir()
    ms.convert_tile(raw, "031313131", pq)
    con = duckdb.connect()
    assert (
        con.execute(f"SELECT confidence FROM read_parquet('{pq}/msbuildings/gb/031313131.parquet')").fetchone()[0]
        == 0.8
    )


def _make_lad(tmp_path):
    """Synthetic LAD: MS polygons A (200x200 m... in BNG), B; four UPRNs."""
    pq = tmp_path / "parquet"
    (pq / "msbuildings" / "gb").mkdir(parents=True)
    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial")
    polys = {"A": (100, 100, 120, 120), "B": (200, 100, 210, 110)}
    rows = []
    for k, (x0, y0, x1, y1) in polys.items():
        rows.append(
            (
                k.lower().ljust(16, "0"),
                f"POLYGON(({x0} {y0},{x1} {y0},{x1} {y1},{x0} {y1},{x0} {y0}))",
                (x1 - x0) * (y1 - y0),
                x0,
                y0,
                x1,
                y1,
            )
        )
    con.execute("CREATE TABLE t(ms_id VARCHAR, w VARCHAR, area_m2 DOUBLE, x0 DOUBLE, y0 DOUBLE, x1 DOUBLE, y1 DOUBLE)")
    con.executemany("INSERT INTO t VALUES (?,?,?,?,?,?,?)", rows)
    con.execute(f"""COPY (SELECT ms_id, ST_AsWKB(ST_GeomFromText(w))::BLOB AS geometry, area_m2,
        {{xmin: x0, ymin: y0, xmax: x1, ymax: y1}} AS bbox FROM t) TO '{pq}/msbuildings/gb/0313.parquet' (FORMAT PARQUET)""")
    out = tmp_path / "out" / "lad=X"
    out.mkdir(parents=True)
    # uprn 1: toid point in A, uprn point outside (toid wins); 2: uprn in A; 3: 3m from B; 4: far away
    con.execute("""CREATE TABLE u AS SELECT * FROM (VALUES
        (1, 300.0, 300.0, 110.0, 110.0, 'o1', 4),
        (2, 105.0, 105.0, NULL, NULL, 'o1', 4),
        (3, 213.0, 105.0, NULL, NULL, NULL, NULL),
        (4, 900.0, 900.0, NULL, NULL, NULL, NULL)) v(uprn, x, y, toid_x, toid_y, building_id, uprns_in_building)""")
    con.execute(f"COPY u TO '{out}/uprn.parquet' (FORMAT PARQUET)")
    con.execute(f"""COPY (SELECT 'o1' AS building_id, ST_AsWKB(ST_GeomFromText('POLYGON((90 90,130 90,130 130,90 130,90 90))'))::BLOB AS geometry)
        TO '{out}/buildings.parquet' (FORMAT PARQUET)""")
    return pq, tmp_path / "out", tmp_path / "staged"


def test_ms_outline_for_lad(tmp_path):
    pq, out, staged = _make_lad(tmp_path)
    stats = ms.ms_outline_for_lad("X", staged, out, pq)
    con = duckdb.connect()
    r = {
        row[0]: row
        for row in con.execute(
            f"SELECT uprn, match_method, ms_building_id, uprns_in_ms_building, ms_area_m2, ms_match_distance_m, "
            f"uprns_in_building, area_ratio_ms_to_ogl FROM read_parquet('{out}/lad=X/outline_ms.parquet')"
        ).fetchall()
    }
    assert r[1][1] == "toid_point_in_polygon" and r[1][3] == 2  # uprns 1 and 2 share A
    assert r[2][1] == "uprn_point_in_polygon" and r[2][2] == r[1][2]
    assert r[3][1] == "nearest_within_5m" and r[3][2].startswith("b") and abs(r[3][5] - 3.0) < 1e-6
    assert r[4][1] == "none" and r[4][2] is None and r[4][3] is None
    assert r[1][4] == 400 and r[1][6] == 4 and r[1][7] == pytest.approx(400 / 1600)
    assert stats["match_method"]["none"]["count"] == 1 and stats["moved_from_block_gt5"]["to_1"] == 0
    d = out / "lad=X"
    assert "CDLA-Permissive-2.0" in (d / "outline_ms.LICENCE.txt").read_text()
    n = duckdb.connect().execute(f"SELECT count(*) FROM read_parquet('{d}/outline_ms_buildings.parquet')").fetchone()[0]
    assert n == 2
    meta = (
        duckdb.connect()
        .execute(f"SELECT value FROM parquet_kv_metadata('{d}/outline_ms_buildings.parquet') WHERE key='geo'")
        .fetchone()
    )
    assert json.loads(meta[0])["columns"]["geometry"]["crs"]["id"]["code"] == 27700


def test_ms_outline_all_resumable(tmp_path):
    pq, out, staged = _make_lad(tmp_path)
    first = ms.ms_outline_all(staged, out, pq)
    assert list(first["built"]) == ["X"]
    second = ms.ms_outline_all(staged, out, pq)
    assert second["skipped"] == ["X"] and not second["built"]
    assert "lad=Y" not in str(ms.ms_outline_all(staged, out, pq, ["Y"])["built"])


# --------------------------------------------------------------------------- review fixes
def test_tile_not_done_if_ni_output_fails(tmp_path, monkeypatch):
    raw = tmp_path / "t.csv.gz"
    _write_tile(raw, [_sq(-0.10, 51.54), _sq(-5.93, 54.60)])
    pq = tmp_path / "parquet"
    pq.mkdir()
    real = ms._geo_meta

    def flaky(epsg, bbox):
        if epsg == 29903:
            raise RuntimeError("NI write failed")
        return real(epsg, bbox)

    monkeypatch.setattr(ms, "_geo_meta", flaky)
    with pytest.raises(RuntimeError, match="NI write failed"):
        ms.convert_tile(raw, "031310131", pq, spans_ni=True)
    assert not (pq / "manifest.json").exists()  # GB was written but must not be manifested yet
    assert not ms.tile_done("031310131", pq)
    assert not list((pq / "msbuildings").rglob("*.parquet"))  # and no unmanifested output is left behind
    monkeypatch.setattr(ms, "_geo_meta", real)
    r = ms.convert_tile(raw, "031310131", pq, spans_ni=True)
    assert r["gb"] == 1 and r["ni"] == 1 and ms.tile_done("031310131", pq)


def test_tile_done_requires_all_expected_outputs(tmp_path):
    raw = tmp_path / "t.csv.gz"
    _write_tile(raw, [_sq(-0.10, 51.54), _sq(-5.93, 54.60)])
    pq = tmp_path / "parquet"
    pq.mkdir()
    ms.convert_tile(raw, "031310131", pq, spans_ni=True)
    assert ms.tile_done("031310131", pq)
    (pq / "msbuildings/ni/031310131.parquet").unlink()
    assert not ms.tile_done("031310131", pq)  # one of the two recorded outputs is gone
    # legacy tile: manifest entry, no .done record -> done while its file exists
    (pq / "msbuildings/031310131.done").unlink()
    assert ms.tile_done("031310131", pq)
    # an empty tile is done once recorded
    e = tmp_path / "e.csv.gz"
    _write_tile(e, [])
    ms.convert_tile(e, "031313131", pq)
    assert ms.tile_done("031313131", pq)


def test_corrupt_manifest_raises_and_is_kept(tmp_path):
    pq = tmp_path / "parquet"
    pq.mkdir()
    (pq / "manifest.json").write_text("{not json")
    with pytest.raises(RuntimeError, match="corrupt"):
        ms._append_manifest(pq, "k", {"a": 1})
    assert (pq / "manifest.json.corrupt").read_text() == "{not json"
    with pytest.raises(RuntimeError):
        (pq / "manifest.json").write_text("{")
        ms.tile_done("031310131", pq)


def test_crash_mid_ms_outline_leaves_no_marker(tmp_path, monkeypatch):
    pq, out, staged = _make_lad(tmp_path)
    ms.ms_outline_for_lad("X", staged, out, pq)
    d = out / "lad=X"
    assert (d / "outline_ms.stats.json").exists()
    real = ms.write_text_atomic

    def dying(path, text):
        if path.name == "outline_ms.LICENCE.txt":
            raise MemoryError("OOM")  # both parquet outputs already rewritten
        return real(path, text)

    monkeypatch.setattr(ms, "write_text_atomic", dying)
    r = ms.ms_outline_all(staged, out, pq, skip_existing=False)
    assert "X" in r["failed"] and not (d / "outline_ms.stats.json").exists()
    monkeypatch.setattr(ms, "write_text_atomic", real)
    assert list(ms.ms_outline_all(staged, out, pq)["built"]) == ["X"]
    assert not list(d.glob("*.tmp"))


def test_ms_outline_all_reruns_when_uprn_newer(tmp_path):
    import os
    import time

    pq, out, staged = _make_lad(tmp_path)
    ms.ms_outline_all(staged, out, pq)
    assert ms.ms_outline_all(staged, out, pq)["skipped"] == ["X"]
    old = time.time() - 1000
    for n in ("outline_ms.parquet", "outline_ms_buildings.parquet", "outline_ms.stats.json"):
        os.utime(out / "lad=X" / n, (old, old))
    assert list(ms.ms_outline_all(staged, out, pq)["built"]) == ["X"]
    assert ms.ms_outline_all(staged, out, pq)["skipped"] == ["X"]
