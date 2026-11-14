import json
import re

from test_build import LAD, staged  # noqa: F401  (fixture)

from uprn_resolver.build import build
from uprn_resolver.qa import qa


def test_qa(staged, tmp_path):  # noqa: F811
    out = tmp_path / "out"
    stats = build(LAD, staged, out)
    rep = tmp_path / "r" / "qa.html"
    s = qa(LAD, out, rep)
    html = rep.read_text()
    assert html.startswith("<!doctype html>") and "leaflet@1.9.4" in html
    assert s["total_uprns"] == stats["total_uprns"] == 6
    assert s["match_method"]["none"]["count"] == 2
    assert s["methods_disagree"]["count"] == 1
    assert s["toid_multiplicity"] == {"0 (no TOID)": 1, "1": 4, "2": 1}
    assert s["sample"]["lines"] == 1 and s["sample"]["buildings"] >= 2
    assert s["suspicious"]["largest_uprns_in_building"][0]["uprns_in_building"] == 2
    d = s["suspicious"]["largest_disagreements"][0]
    assert d["uprn"] == 3 and abs(d["dist_m"] - 100.0) < 0.1
    assert {r["uprn"] for r in s["suspicious"]["point_without_building"]} == {4, 5}
    assert "Total UPRNs" in html and "toid_point_in_polygon" in html
    assert "XX1 1XX" not in html
    # embedded GeoJSON is valid and in WGS84 (synthetic coords are near the BNG origin)
    m = re.search(r"const D=(.*?), C=", html, re.S)
    geo = json.loads(m.group(1).replace("<\\/", "</"))
    lon, lat = geo["uprns"]["features"][0]["geometry"]["coordinates"]
    assert -10 < lon < 3 and 49 < lat < 52


def test_qa_parcels_and_street(staged, tmp_path):  # noqa: F811
    import pyarrow as pa
    import pyarrow.parquet as pq
    from test_build import sq

    d = staged / LAD
    pq.write_table(
        pa.table(
            {
                "uprn": [1],
                "usrn": [1],
                "street_name": ["Test Street"],
                "street_name_alt": [None],
                "locality": [None],
                "street_method": ["m"],
                "usrn_count": [1],
            }
        ),
        d / "streets.parquet",
    )
    pq.write_table(
        pa.table({"uprn": [1], "parcel_inspire_id": ["p1"], "parcel_area_m2": [100.0], "uprns_in_parcel": [1]}),
        d / "inspire_parcel.parquet",
    )
    pq.write_table(pa.table({"parcel_inspire_id": ["p1"], "geom_wkb": [sq(0, 0)]}), d / "inspire_polygons.parquet")
    out = tmp_path / "out"
    build(LAD, staged, out)
    s = qa(LAD, out, tmp_path / "qa.html")
    assert s["sample"]["parcels"] == 1 and s["with_street_name"]["count"] == 1
    assert "Test Street" in (tmp_path / "qa.html").read_text()
    assert "Outline granularity" in (tmp_path / "qa.html").read_text()
