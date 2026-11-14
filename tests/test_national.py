import json

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from uprn_resolver.build import _geo_meta
from uprn_resolver.national import national

_WKB = bytes.fromhex(
    "0103000000010000000400000000000000000000000000000000000000000000000000F03F"
    "0000000000000000000000000000F03F000000000000F03F00000000000000000000000000000000"
)


def _lad(out, lad, uprns, blds, stats):
    d = out / f"lad={lad}"
    d.mkdir(parents=True)
    pq.write_table(
        pa.table(
            {
                "uprn": [u[0] for u in uprns],
                "building_id": [u[1] for u in uprns],
                "uprns_in_building": [u[2] for u in uprns],
                "match_method": ["toid_point_in_polygon" if u[1] else "none" for u in uprns],
            }
        ),
        d / "uprn.parquet",
    )
    t = pa.table({"building_id": blds, "geometry": [_WKB] * len(blds)})
    t = t.replace_schema_metadata({"geo": _geo_meta(["Polygon"], [0, 0, 1, 1])})
    pq.write_table(t, d / "buildings.parquet")
    (d / "stats.json").write_text(json.dumps(stats))


def _stats(lad, total, none, shared):
    return {
        "lad": lad,
        "total_uprns": total,
        "match_method": {"toid_point_in_polygon": {"count": total - none, "pct": 0}, "none": {"count": none, "pct": 0}},
        "with_street_name": {"count": total, "pct": 100.0},
        "with_parcel": {"count": 1, "pct": 0},
        "outline_granularity": {"single": 0, "parcel_finer": 0, "shared": shared, "none": none},
        "uprns_in_building": {"1": 1, "2-5": 1},
        "epc_present": False,
    }


def test_national(tmp_path):
    out = tmp_path / "out"
    _lad(out, "A", [(1, "b1", 2), (2, "b1", 2), (3, "bs", 1), (4, None, None)], ["b1", "bs"], _stats("A", 4, 1, 3))
    _lad(out, "B", [(5, "bs", 1), (6, "b2", 1)], ["bs", "b2"], _stats("B", 2, 0, 0))
    rep = tmp_path / "r" / "national.html"
    s = national(out, rep)
    assert s["lads"] == 2 and s["total_uprns"] == 6
    assert s["match_method"]["none"] == {"count": 1, "pct": round(100 / 6, 4)}
    assert s["with_street_name"]["count"] == 6 and s["with_street_name"]["pct"] == 100.0
    c = s["cross_lad"]
    assert c["buildings_national"] == 3 and c["duplicate_building_rows"] == 1
    assert c["buildings_spanning_multiple_lads"] == 1
    assert c["uprns_in_spanning_buildings"] == 2 and c["uprns_with_changed_count"] == 2
    n = out / "national"
    con = duckdb.connect()
    r = con.execute(f"SELECT * FROM '{n}/building_uprn_counts.parquet' ORDER BY building_id").fetchall()
    assert r == [("b1", 2, 1), ("b2", 1, 1), ("bs", 2, 2)]
    assert con.execute(f"SELECT count(*) FROM '{n}/buildings.parquet'").fetchone()[0] == 3
    geo = json.loads(pq.read_schema(n / "buildings.parquet").metadata[b"geo"])
    assert geo["primary_column"] == "geometry"
    ls = con.execute(f"SELECT lad, total_uprns, pct_none FROM '{n}/lad_summary.parquet' ORDER BY lad").fetchall()
    assert ls[0][:2] == ("A", 4) and ls[1][2] == 0.0
    assert json.loads((n / "stats.json").read_text())["lads"] == 2
    assert rep.read_text().startswith("<!doctype html>") and "Per-LAD" in rep.read_text()
