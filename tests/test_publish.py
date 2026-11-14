import json
import subprocess

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from shapely import wkb
from shapely.geometry import Point, box

from uprn_resolver import publish as P

GEO = json.dumps(
    {
        "version": "1.1.0",
        "primary_column": "geometry",
        "columns": {"geometry": {"encoding": "WKB", "geometry_types": []}},
    }
)


def _write(path, cols, geo=True):
    t = pa.table(cols)
    if geo:
        t = t.replace_schema_metadata({"geo": GEO})
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(t, path)


def _points(n, x0):
    return [wkb.dumps(Point(x0 + (i * 37) % 11 * 100.0, 180000 + (i * 17) % 13 * 100.0)) for i in range(n)]


def _lad(o, lad, x0, n=40, extra=True):
    d = o / f"lad={lad}"
    _write(d / "uprn.parquet", {"uprn": list(range(n)), "geometry": _points(n, x0), "street_name": ["a"] * n})
    _write(
        d / "buildings.parquet",
        {"building_id": ["a", "b"], "geometry": [wkb.dumps(box(x0, 1, x0 + 5, 6)), wkb.dumps(box(x0, 9, x0 + 5, 14))]},
    )
    (d / "stats.json").write_text(json.dumps({"n": n}))
    _write(d / "address_local.parquet", {"uprn": [1], "full_address": ["secret"]}, geo=False)
    if extra:
        _write(d / "epc.parquet", {"uprn": [3, 1], "epc_count": [1, 2]}, geo=False)
        _write(d / "poi.parquet", {"poi_id": ["p"], "name": ["Shop"], "uprn": [1]}, geo=False)
        (d / "poi.LICENCE.txt").write_text("Overture")
        _write(
            d / "provenance.parquet",
            {
                "uprn": [2, 1],
                "street_name_fallback": ["x", "y"],
                "postcode_onsud": ["AB1 1AB", "AB1 1AC"],
                "postcode_flag": ["ok", "ok"],
            },
            geo=False,
        )
        _write(
            d / "streets_odbl.parquet",
            {"uprn": [1], "osm_street_name": ["High St"], "osm_addr_postcode": ["AB1"]},
            geo=False,
        )
        (d / "streets_odbl.LICENCE.txt").write_text("ODbL")
        _write(
            d / "resolved_open.parquet",
            {
                "uprn": [1, 0],
                "geometry": _points(2, x0),
                "street_name": ["a", "b"],
                "street_source": ["os", "os"],
                "locality": ["l", "m"],
                "best_outline_source": ["openmap_building", None],
                "poi_name": ["Shop", None],
            },
        )
        (d / "resolved_open.LICENCE.txt").write_text("OGL+Overture")
        _write(d / "outline_ms.parquet", {"uprn": [2, 1], "ms_building_id": ["m", "m"]}, geo=False)
        _write(
            d / "outline_ms_buildings.parquet",
            {"ms_building_id": ["m"], "geometry": [wkb.dumps(box(x0, 1, x0 + 5, 6))]},
        )
        (d / "outline_ms.LICENCE.txt").write_text("CDLA")
        _write(
            d / "outline_odbl.parquet",
            {"uprn": [2, 1], "osm_building_id": ["o", "o"], "osm_building_has_addr": [False, True]},
            geo=False,
        )
        _write(
            d / "outline_odbl_buildings.parquet",
            {"osm_building_id": ["o"], "geometry": [wkb.dumps(box(x0, 1, x0 + 5, 6))]},
        )
        (d / "outline_odbl.LICENCE.txt").write_text("ODbL-outline")
        _write(d / "resolved_local.parquet", {"uprn": [1], "postcode": ["X"]}, geo=False)


@pytest.fixture
def out(tmp_path):
    o = tmp_path / "out"
    _lad(o, "E1", 500000)
    _lad(o, "E2", 600000)
    return o


@pytest.fixture
def release(out, tmp_path):
    files = P.assemble(out, "2026-10", tmp_path / "rel", threads=1)
    return tmp_path / "rel" / "2026-10", files


def test_layout(release):
    root, files = release
    for rel in (
        "uprn/lad=E1/uprn.parquet",
        "uprn/lad=E2/uprn.parquet",
        "buildings/lad=E1/buildings.parquet",
        "epc/lad=E1/epc.parquet",
        "poi/lad=E1/poi.parquet",
        "poi/LICENCE.txt",
        "provenance/lad=E2/provenance.parquet",
        "streets_odbl/lad=E1/streets_odbl.parquet",
        "streets_odbl/LICENCE.txt",
        "lad_index.parquet",
        "stats.json",
        "README.md",
        "LICENCE.md",
    ):
        assert (root / rel).exists(), rel
    assert json.loads((root / "stats.json").read_text()) == {"E1": {"n": 40}, "E2": {"n": 40}}
    assert not [f for f in root.rglob("*") if "address_local" in f.name]
    for rel in (
        "resolved_open/lad=E1/resolved_open.parquet",
        "resolved_open/LICENCE.txt",
        "outline_ms/lad=E2/outline_ms.parquet",
        "outline_ms/LICENCE.txt",
        "outline_ms_buildings/lad=E1/outline_ms_buildings.parquet",
        "outline_ms_buildings/LICENCE.txt",
        "outline_odbl/lad=E1/outline_odbl.parquet",
        "outline_odbl/LICENCE.txt",
        "outline_odbl_buildings/lad=E1/outline_odbl_buildings.parquet",
        "outline_odbl_buildings/LICENCE.txt",
    ):
        assert (root / rel).exists(), rel
    assert (root / "outline_ms" / "LICENCE.txt").read_text() == "CDLA"
    assert (root / "outline_odbl_buildings" / "LICENCE.txt").read_text() == "ODbL-outline"
    assert (root / "resolved_open" / "LICENCE.txt").read_text() == "OGL+Overture"
    assert not [f for f in root.rglob("*") if "resolved_local" in f.name]
    for rel in (
        "resolved_open/lad=E1/resolved_open.parquet",
        "outline_ms_buildings/lad=E1/outline_ms_buildings.parquet",
        "outline_odbl_buildings/lad=E1/outline_odbl_buildings.parquet",
    ):
        assert P.validate_geoparquet(root / rel) == [], rel
    assert (root / "streets_odbl" / "LICENCE.txt").read_text() == "ODbL"
    assert (root / "poi" / "LICENCE.txt").read_text() == "Overture"


def test_geo_metadata_and_covering(release):
    root, _ = release
    f = root / "uprn/lad=E1/uprn.parquet"
    geo = json.loads(pq.read_schema(f).metadata[b"geo"])
    c = geo["columns"]["geometry"]
    assert geo["version"] == "1.1.0" and c["encoding"] == "WKB" and c["geometry_types"] == ["Point"]
    assert c["crs"]["id"] == {"authority": "EPSG", "code": 27700}
    assert c["covering"]["bbox"]["xmin"] == ["bbox", "xmin"]
    assert pq.read_schema(f).field("bbox").type.num_fields == 4
    assert P.validate_geoparquet(f) == []
    assert P.validate_geoparquet(root / "buildings/lad=E1/buildings.parquet") == []
    assert json.loads(pq.read_schema(root / "buildings/lad=E1/buildings.parquet").metadata[b"geo"])["columns"][
        "geometry"
    ]["geometry_types"] == ["Polygon"]


def test_row_groups_and_hilbert(tmp_path):
    o = tmp_path / "out"
    _lad(o, "E1", 500000, n=5000, extra=False)
    P.assemble(o, "r", tmp_path / "rel", row_group_size=2048, threads=1)
    f = tmp_path / "rel/r/uprn/lad=E1/uprn.parquet"
    rep = P.file_report(f)
    assert rep["row_groups"] == 3 and rep["codecs"] == ["ZSTD"] and rep["rows"] == 5000
    t = pq.read_table(f).to_pydict()
    assert sorted(t["uprn"]) == list(range(5000))
    assert t["uprn"] != list(range(5000))  # re-ordered
    assert t["bbox"][0]["xmin"] == wkb.loads(t["geometry"][0]).x


def test_plain_tables(release):
    root, _ = release
    prov = pq.read_table(root / "provenance/lad=E1/provenance.parquet")
    assert "postcode_onsud" not in prov.column_names and "postcode_flag" in prov.column_names
    assert prov["uprn"].to_pylist() == [1, 2]
    assert (
        not pq.read_schema(root / "epc/lad=E1/epc.parquet").metadata
        or b"geo" not in pq.read_schema(root / "epc/lad=E1/epc.parquet").metadata
    )
    idx = pq.read_table(root / "lad_index.parquet").to_pylist()
    assert {r["lad"] for r in idx} == {"E1", "E2"} and all(r["n_uprn"] == 40 for r in idx)


def test_postcode_kept_when_allowed(out, tmp_path):
    P.assemble(out, "r", tmp_path / "rel", allow=("postcode",), threads=1)
    prov = pq.read_table(tmp_path / "rel/r/provenance/lad=E1/provenance.parquet")
    assert "postcode_onsud" in prov.column_names


def test_forbidden_column(out, tmp_path):
    _write(out / "lad=E1" / "uprn.parquet", {"uprn": [1], "paon": ["1"], "geometry": _points(1, 5e5)})
    with pytest.raises(P.PublishError, match="paon"):
        P.assemble(out, "r", tmp_path / "rel")


def test_postcode_needs_allow(out, tmp_path):
    _write(out / "lad=E1" / "uprn.parquet", {"uprn": [1], "postcode": ["X"], "geometry": _points(1, 5e5)})
    with pytest.raises(P.PublishError):
        P.assemble(out, "r", tmp_path / "rel")
    P.assemble(out, "r", tmp_path / "rel", allow=("postcode",), threads=1)


def test_odbl_requires_licence(out, tmp_path):
    (out / "lad=E1" / "streets_odbl.LICENCE.txt").unlink()
    with pytest.raises(P.PublishError, match="LICENCE"):
        P.assemble(out, "r", tmp_path / "rel")


def test_lad_filter_and_optional_tables(tmp_path):
    o = tmp_path / "out"
    _lad(o, "E1", 5e5, extra=False)
    _lad(o, "E2", 6e5, extra=False)
    root = tmp_path / "rel"
    P.assemble(o, "r", root, lads=["E2"], threads=1)
    assert (root / "r/uprn/lad=E2/uprn.parquet").exists() and not (root / "r/uprn/lad=E1").exists()
    assert not (root / "r/poi").exists() and not (root / "r/streets_odbl").exists()


def _rewrite_geo(src, dst, mutate):
    t = pq.read_table(src)
    geo = json.loads(t.schema.metadata[b"geo"])
    mutate(geo)
    pq.write_table(t.replace_schema_metadata({"geo": json.dumps(geo)}), dst)


@pytest.mark.parametrize(
    "mutate,msg",
    [
        (lambda g: g["columns"]["geometry"].pop("crs"), "crs absent"),
        (lambda g: g["columns"]["geometry"].pop("covering"), "no covering"),
        (lambda g: g["columns"]["geometry"].update(geometry_types=["Polygon"]), "geometry_types"),
        (lambda g: g.update(version="1.0.0"), "1.1"),
        (lambda g: g["columns"]["geometry"].update(bbox=[0, 0, 1, 1]), "bbox"),
        (lambda g: g["columns"]["geometry"].update(encoding="WKT"), "encoding"),
        (lambda g: g["columns"]["geometry"]["covering"]["bbox"].pop("ymax"), "covering"),
    ],
)
def test_validator_catches(release, tmp_path, mutate, msg):
    root, _ = release
    bad = tmp_path / "bad.parquet"
    _rewrite_geo(root / "uprn/lad=E1/uprn.parquet", bad, mutate)
    assert any(msg in p for p in P.validate_geoparquet(bad))


def test_validator_catches_bad_bbox_values(release, tmp_path):
    root, _ = release
    t = pq.read_table(root / "uprn/lad=E1/uprn.parquet")
    i = t.schema.get_field_index("bbox")
    bb = t["bbox"].to_pylist()
    bb[0]["xmax"] += 50
    t = t.set_column(i, t.schema.field("bbox"), pa.array(bb, type=t.schema.field("bbox").type))
    pq.write_table(t, tmp_path / "bad.parquet")
    assert any("covering bbox disagrees" in p for p in P.validate_geoparquet(tmp_path / "bad.parquet"))


def test_bbox_query_report(release):
    root, _ = release
    r = P.bbox_query_report(root / "uprn/lad=E1/uprn.parquet", (500000, 180000, 500500, 180500))
    assert r["covering"]["count"] == r["wkb_scan"]["count"] > 0
    assert r["covering"]["row_groups_scanned"]


def test_missing_geo(out, tmp_path):
    _write(out / "lad=E2" / "buildings.parquet", {"building_id": ["a"]}, geo=False)
    with pytest.raises(P.PublishError, match="geo"):
        P.assemble(out, "r", tmp_path / "rel")


def test_dry_run_uploads_nothing(out, tmp_path, monkeypatch, capsys):
    def boom(*a, **k):
        raise AssertionError("subprocess called")

    monkeypatch.setattr(subprocess, "run", boom)
    actions = P.publish(out, "2026-10", "s3://acct/prod/", stage_dir=tmp_path / "rel")
    assert "aws s3 sync" in actions[1] and "s3://acct/prod/2026-10/" in actions[1]
    assert "data.source.coop" in actions[1]
    assert "[dry-run]" in capsys.readouterr().out


def test_bad_dest(out):
    with pytest.raises(P.PublishError):
        P.publish(out, "r", "acct/prod")


@pytest.mark.parametrize("table", ["outline_ms", "outline_odbl", "resolved_open", "poi"])
def test_sidecar_required(out, tmp_path, table):
    (out / "lad=E1" / f"{table}.LICENCE.txt").unlink()
    with pytest.raises(P.PublishError, match="LICENCE"):
        P.assemble(out, "r", tmp_path / "rel")


def test_never_ship_local_tables(release):
    root, files = release
    assert not [f for f in files if "resolved_local" in str(f) or "address_local" in str(f)]
    assert not (root / "resolved_local").exists() and not (root / "address_local").exists()
    assert set(P.NEVER_SHIP) == {"address_local", "resolved_local", "ppd_local"}
    assert not set(P.NEVER_SHIP) & {*P.GEO_TABLES, *P.OPTIONAL_GEO, *P.PLAIN_TABLES, P.ODBL_TABLE}


def test_resolved_open_forbidden_columns(out, tmp_path):
    base = {"uprn": [1], "geometry": _points(1, 5e5)}
    for col in ("single_line_address", "postcode", "address_licence"):
        _write(out / "lad=E1" / "resolved_open.parquet", {**base, col: ["x"]})
        with pytest.raises(P.PublishError, match=col):
            P.assemble(out, "r", tmp_path / "rel")


def test_odbl_never_in_ogl_tables(out, tmp_path):
    base = {"uprn": [1], "geometry": _points(1, 5e5)}
    _write(out / "lad=E1" / "resolved_open.parquet", {**base, "osm_building_id": ["o"]})
    with pytest.raises(P.PublishError, match="ODbL-derived column"):
        P.assemble(out, "r", tmp_path / "rel")
    _write(out / "lad=E1" / "resolved_open.parquet", {**base, "best_outline_source": ["osm_building"]})
    with pytest.raises(P.PublishError, match="best_outline_source"):
        P.assemble(out, "r", tmp_path / "rel")
    _write(out / "lad=E1" / "uprn.parquet", {**base, "osm_street_name": ["x"]})
    with pytest.raises(P.PublishError, match="osm_street_name"):
        P.assemble(out, "r", tmp_path / "rel")


def test_odbl_tables_separate_folders(release):
    root, _ = release
    for t in ("streets_odbl", "outline_odbl", "outline_odbl_buildings"):
        cols = pq.read_schema(next((root / t).glob("lad=E1/*.parquet"))).names
        assert t in P.ODBL_TABLES and "uprn" in cols or t == "outline_odbl_buildings"
    for t in P.OGL_TABLES:
        for f in (root / t).glob("lad=*/*.parquet"):
            assert not [c for c in pq.read_schema(f).names if c.startswith("osm_")], f


# --------------------------------------------------------------------------- review fixes
def test_assemble_starts_from_clean_release_dir(out, tmp_path):
    stage = tmp_path / "rel"
    P.assemble(out, "2026-10", stage, threads=1)
    root = stage / "2026-10"
    assert (root / "uprn" / "lad=E2" / "uprn.parquet").exists()
    stale_lad = root / "uprn" / "lad=E9"
    stale_lad.mkdir()
    (stale_lad / "uprn.parquet").write_bytes(b"old")
    (root / "epc" / "lad=E1" / "epc.parquet.tmp").write_bytes(b"crashed")
    (root / "address_old.txt").write_text("stale")
    P.assemble(out, "2026-10", stage, lads=["E1"], threads=1, force=True)
    names = {str(f.relative_to(root)) for f in root.rglob("*") if f.is_file()}
    assert not any("E2" in n or "E9" in n or n.endswith(".tmp") or "address_old" in n for n in names)
    assert "uprn/lad=E1/uprn.parquet" in names


@pytest.mark.parametrize("release_name", ["", "..", "../x", "a/b", ".hidden"])
def test_assemble_refuses_unsafe_release_names(out, tmp_path, release_name):
    victim = tmp_path / "x"
    victim.mkdir()
    (victim / "keep.txt").write_text("precious")
    with pytest.raises(P.PublishError, match="unsafe|not directly|overlaps"):
        P.assemble(out, release_name, tmp_path / "rel", threads=1)
    assert (victim / "keep.txt").exists()


def test_assemble_refuses_release_inside_out_dir(out):
    before = sorted(p.name for p in out.iterdir())
    with pytest.raises(P.PublishError, match="overlaps"):
        P.assemble(out, "lad=E1", out, threads=1)
    assert sorted(p.name for p in out.iterdir()) == before


def test_never_ship_is_content_based(out, tmp_path):
    # name looks harmless (an ODbL-lane table with a blanket column allowance) but carries local-only columns
    d = out / "lad=E1"
    _write(
        d / "outline_ms.parquet",
        {"uprn": [1], "ms_building_id": ["m"], "single_line_address": ["1 High St"], "best_outline_id_local": ["x"]},
        geo=False,
    )
    with pytest.raises(P.PublishError, match="local-only column"):
        P.assemble(out, "2026-10", tmp_path / "rel", threads=1)


def test_check_release_tree_flags_tmp_and_unknown_tables(tmp_path):
    root = tmp_path / "r"
    (root / "uprn" / "lad=E1").mkdir(parents=True)
    (root / "uprn" / "lad=E1" / "uprn.parquet.tmp").write_bytes(b"x")
    _write(root / "mystery" / "m.parquet", {"uprn": [1], "address_source": ["epc"]}, geo=False)
    probs = "\n".join(P.check_release_tree(root))
    assert "leftover temporary file" in probs and "outside the known release tables" in probs
    assert "address_source" in probs


def test_release_geo_has_single_bbox_and_no_hive_lad_column(out, tmp_path):
    d = out / "lad=E1"
    pts = _points(1, 500000)
    geom = wkb.loads(pts[0])
    bbox = pa.array(
        [{"xmin": geom.x, "ymin": geom.y, "xmax": geom.x, "ymax": geom.y}],
        type=pa.struct([(k, pa.float64()) for k in ("xmin", "ymin", "xmax", "ymax")]),
    )
    _write(d / "outline_ms_buildings.parquet", {"ms_building_id": ["m"], "geometry": pts, "bbox": bbox})
    files = P.assemble(out, "2026-10", tmp_path / "rel", threads=1)
    root = tmp_path / "rel" / "2026-10"
    names = pq.read_schema(root / "outline_ms_buildings" / "lad=E1" / "outline_ms_buildings.parquet").names
    assert names.count("bbox") == 1 and "bbox_1" not in names
    for f in files:
        if f.suffix == ".parquet" and f.name != "lad_index.parquet":
            assert "lad" not in pq.read_schema(f).names or f.name == "resolved_open.parquet", f
    assert "lad" not in pq.read_schema(root / "uprn" / "lad=E1" / "uprn.parquet").names
    assert "lad" not in pq.read_schema(root / "epc" / "lad=E1" / "epc.parquet").names


# --------------------------------------------------------------------------- versioning, manifest, national, changelog
def test_release_is_immutable_unless_force(out, tmp_path):
    stage = tmp_path / "rel"
    P.assemble(out, "2026-10", stage, threads=1)
    with pytest.raises(P.PublishError, match="immutable"):
        P.assemble(out, "2026-10", stage, threads=1)
    P.assemble(out, "2026-10", stage, threads=1, force=True)
    # a failed run leaves no partial folder behind
    _write(out / "lad=E1" / "resolved_open.parquet", {"uprn": [1], "geometry": _points(1, 5e5), "osm_x": [1]})
    with pytest.raises(P.PublishError):
        P.assemble(out, "2026-11", stage, threads=1)
    assert not (stage / "2026-11").exists()


def test_manifest_and_latest(release, tmp_path):
    import hashlib

    root, files = release
    m = json.loads((root / "release_manifest.json").read_text())
    by = {e["path"]: e for e in m["files"]}
    assert "release_manifest.json" not in by and "stac/catalog.json" in by
    e = by["uprn/lad=E1/uprn.parquet"]
    assert e["rows"] == 40 and e["sha256"] == hashlib.sha256((root / e["path"]).read_bytes()).hexdigest()
    assert e["bytes"] == (root / e["path"]).stat().st_size
    assert m["tables"]["uprn"]["rows"] == 80 and m["n_files"] == len(m["files"])
    latest = json.loads((root.parent / "latest.json").read_text())
    assert latest["latest"] == "2026-10" and latest["manifest"] == "2026-10/release_manifest.json"
    assert root / "release_manifest.json" in files


def test_resolved_open_epc_columns_allowed(out, tmp_path):
    extra = {
        "epc_lmk_key": ["k", "j"],
        "epc_certificate_type": ["domestic", None],
        "epc_lodgement_date": ["2020-01-01", None],
        "epc_count": [1, 0],
    }
    _write(
        out / "lad=E1" / "resolved_open.parquet",
        {"uprn": [1, 0], "geometry": _points(2, 5e5), "street_name": ["a", "b"], **extra},
    )
    P.assemble(out, "r", tmp_path / "rel", threads=1)
    names = pq.read_schema(tmp_path / "rel" / "r" / "resolved_open" / "lad=E1" / "resolved_open.parquet").names
    assert set(extra) <= set(names)


def test_national_files(release, tmp_path):
    root, _ = release
    got = P.write_national(root, ("uprn", "buildings"), threads=1)
    assert [g.relative_to(root).as_posix() for g in got] == [
        "national/uprn/part-0000.parquet",
        "national/buildings/part-0000.parquet",
    ]
    assert P.validate_geoparquet(got[0]) == [] and P.validate_geoparquet(got[1]) == []
    assert pq.ParquetFile(got[0]).metadata.num_rows == 80
    assert pq.ParquetFile(got[1]).metadata.num_rows == 2  # ids a, b appear in both LADs: deduplicated
    assert P.check_release_tree(root) == []


def test_national_files_split_into_parts(release):
    root, _ = release
    got = P.write_national(root, ("uprn",), threads=1, part_bytes=1500)
    assert len(got) >= 2
    assert sum(pq.ParquetFile(g).metadata.num_rows for g in got) == 80
    assert all(P.validate_geoparquet(g) == [] for g in got)


def test_assemble_options_national_and_changelog(out, tmp_path):
    stage = tmp_path / "rel"
    P.assemble(out, "2026-09", stage, threads=1, stac=False)
    P.assemble(out, "2026-10", stage, threads=1, national_files=True, changelog_against=stage / "2026-09")
    root = stage / "2026-10"
    assert (root / "national" / "uprn" / "part-0000.parquet").exists()
    assert (root / "CHANGELOG.json").exists() and (root / "CHANGELOG.md").exists()
    assert not (stage / "2026-09" / "stac").exists()
    assert json.loads((stage / "latest.json").read_text())["latest"] == "2026-10"


def _mini_release(root, uprns, outline, street):
    d = root / "uprn" / "lad=E1"
    _write(d / "uprn.parquet", {"uprn": uprns, "street_name": street}, geo=False)
    d = root / "resolved_open" / "lad=E1"
    _write(
        d / "resolved_open.parquet",
        {"uprn": uprns, "best_outline_id": outline, "outline_confidence": ["high"] * len(uprns), "street_name": street},
        geo=False,
    )


def test_diff_releases(tmp_path):
    old, new = tmp_path / "2026-09", tmp_path / "2026-10"
    _mini_release(old, [1, 2, 3, 4], ["a", "b", None, "d"], ["x", "y", "z", "w"])
    _mini_release(new, [2, 3, 4, 5, 6], ["b", "c", "d2", "e", "f"], ["y", "z", "w2", "v", None])
    r = P.diff_releases(old, new, write=True, threads=1)
    u, ro = r["tables"]["uprn"], r["tables"]["resolved_open"]
    assert (u["added"], u["removed"]) == (2, 1) and u["sample_added"] == [5, 6] and u["sample_removed"] == [1]
    assert u["changed"]["street_name"] == 1  # uprn 4
    assert ro["changed"] == {"best_outline_id": 2, "outline_confidence": 0, "street_name": 1}  # 3 (NULL->c), 4
    assert json.loads((new / "CHANGELOG.json").read_text())["tables"]["uprn"]["added"] == 2
    md = (new / "CHANGELOG.md").read_text()
    assert "2026-09 to 2026-10" in md and "| uprn | 4 | 5 | 2 | 1 |" in md


def test_diff_reports_table_added(tmp_path):
    old, new = tmp_path / "a", tmp_path / "b"
    _mini_release(old, [1], ["a"], ["x"])
    _mini_release(new, [1], ["a"], ["x"])
    import shutil

    shutil.rmtree(old / "resolved_open")
    assert P.diff_releases(old, new, threads=1)["tables"]["resolved_open"] == {"status": "added_table"}


def _ppd_files(d, local=True):
    _write(
        d / "ppd_open.parquet",
        {
            "uprn": [1, 2],
            "transaction_id": ["{A}", "{B}"],
            "price": [100, 200],
            "date": ["2026-09-01", "2026-09-02"],
            "property_type": ["T", "F"],
            "old_new": ["N", "N"],
            "duration": ["F", "L"],
            "ppd_category_type": ["A", "A"],
        },
        geo=False,
    )
    _write(
        d / "ppd_open_summary.parquet",
        {"uprn": [1, 2], "sales_count": [1, 1], "last_price": [100, 200]},
        geo=False,
    )
    (d / "ppd_open.LICENCE.txt").write_text("Contains HM Land Registry data")
    if local:
        _write(d / "ppd_local.parquet", {"uprn": [1], "paon": ["1"], "street": ["High St"]}, geo=False)
        _write(d / "ppd_local_summary.parquet", {"uprn": [1], "sales_count": [1]}, geo=False)
        (d / "ppd_local.LICENCE.txt").write_text("LOCAL USE ONLY")
        (d / "ppd.stats.json").write_text("{}")


def test_ppd_open_collected_ppd_local_never(out, tmp_path):
    _ppd_files(out / "lad=E1")
    files = P.assemble(out, "r", tmp_path / "rel")
    root = tmp_path / "rel" / "r"
    assert (root / "ppd_open" / "lad=E1" / "ppd_open.parquet").exists()
    assert (root / "ppd_open_summary" / "lad=E1" / "ppd_open_summary.parquet").exists()
    assert (root / "ppd_open" / "LICENCE.txt").read_text().startswith("Contains HM Land Registry")
    assert (root / "ppd_open_summary" / "LICENCE.txt").exists()
    assert not [f for f in files if "ppd_local" in str(f) or f.name == "ppd.stats.json"]
    assert not [f for f in root.rglob("*") if "ppd_local" in str(f) or f.name == "ppd.stats.json"]
    assert P.validate_parquet(root / "ppd_open" / "lad=E1" / "ppd_open.parquet", require_geo=False, ogl=True) == []


def test_ppd_open_needs_sidecar(out, tmp_path):
    _ppd_files(out / "lad=E1", local=False)
    (out / "lad=E1" / "ppd_open.LICENCE.txt").unlink()
    with pytest.raises(P.PublishError, match="LICENCE"):
        P.assemble(out, "r", tmp_path / "rel")


def test_ppd_local_rejected_in_release_tree(tmp_path):
    root = tmp_path / "r"
    _write(root / "ppd_local" / "lad=E1" / "ppd_local.parquet", {"uprn": [1]}, geo=False)
    _write(root / "ppd_open" / "lad=E1" / "ppd_open.parquet", {"uprn": [1], "street": ["x"]}, geo=False)
    probs = P.check_release_tree(root)
    assert any("local-only table" in p for p in probs)


def test_lean_release(out, tmp_path):
    """LEAN_TABLES ships only the answer per UPRN and its shapes; MS footprints are cut to the used ones."""
    for lad, x0 in (("E1", 500000), ("E2", 600000)):
        d = out / f"lad={lad}"
        _write(
            d / "resolved_open.parquet",
            {
                "uprn": [1, 0],
                "geometry": _points(2, x0),
                "street_name": ["a", "b"],
                "street_source": ["os", "os"],
                "locality": ["l", "m"],
                "best_outline_source": ["ms_building", None],
                "best_outline_id": ["m", None],
                "poi_name": ["Shop", None],
            },
        )
        _write(
            d / "outline_ms_buildings.parquet",
            {
                "ms_building_id": ["m", "unused"],
                "geometry": [wkb.dumps(box(x0, 1, x0 + 5, 6)), wkb.dumps(box(x0 + 10, 1, x0 + 15, 6))],
            },
        )
    P.assemble(out, "r", tmp_path / "rel", threads=1, stac=False, lean=True)
    root = tmp_path / "rel" / "r"
    shipped = {p.name for p in root.iterdir() if p.is_dir()}
    assert shipped == {"uprn", "buildings", "resolved_open", "outline_ms_buildings"}
    ids = pq.read_table(root / "outline_ms_buildings" / "lad=E1" / "outline_ms_buildings.parquet").column(
        "ms_building_id"
    )
    assert ids.to_pylist() == ["m"]
    assert (root / "outline_ms_buildings" / "LICENCE.txt").exists()
    assert not (tmp_path / "rel" / ".ms_used").exists()
