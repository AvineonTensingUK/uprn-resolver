import json

import pystac
import pytest

from tests.test_publish import _lad
from uprn_resolver import publish as P
from uprn_resolver import stac


@pytest.fixture(scope="module")
def rel(tmp_path_factory):
    t = tmp_path_factory.mktemp("stac")
    out = t / "out"
    _lad(out, "E1", 500000)
    _lad(out, "E2", 600000)
    P.assemble(out, "2026-10", t / "rel", threads=1, stac=False)
    root = t / "rel" / "2026-10"
    return root, stac.build_stac(root, validate=False)


def test_bbox_conversion_covers_corners():
    box, ring = stac.bng_bbox_to_wgs84([530000, 180000, 531000, 181000])
    assert -0.2 < box[0] < box[2] < 0.0 and 51.4 < box[1] < box[3] < 51.6
    assert ring[0] == ring[-1] and all(box[0] <= x <= box[2] and box[1] <= y <= box[3] for x, y in ring)


def test_release_datetime():
    assert stac.release_datetime("2026-10").isoformat() == "2026-10-01T00:00:00+00:00"
    assert stac.release_datetime("2026-10-23").day == 23


def test_structure_and_licences(rel):
    root, summary = rel
    assert summary["collections"] == 11 and summary["items"] == 11 * 2
    cat = pystac.Catalog.from_file(str(root / "stac" / "catalog.json"))
    colls = {c.id: c for c in cat.get_children()}
    assert colls["uprn"].license == "OGL-UK-3.0" and colls["streets_odbl"].license == "ODbL-1.0"
    assert colls["outline_ms"].license == "CDLA-Permissive-2.0" and colls["poi"].license == "various"
    assert colls["resolved_open"].license == "other"
    lic = [lk.target for lk in colls["resolved_open"].links if lk.rel == "license"]
    assert lic == ["../../resolved_open/LICENCE.txt"]
    assert colls["uprn"].extra_fields["table:columns"][0]["name"] == "uprn"


def test_items_assets_and_bbox(rel):
    root, _ = rel
    cat = pystac.Catalog.from_file(str(root / "stac" / "catalog.json"))
    item = cat.get_item("uprn-E1", recursive=True)
    assert item.datetime.year == 2026 and item.properties["table:row_count"] == 40
    assert item.properties["proj:code"] == "EPSG:27700"
    a = item.assets["data"]
    assert a.media_type == "application/vnd.apache.parquet" and not a.href.startswith("/")
    assert a.get_absolute_href().endswith("uprn/lad=E1/uprn.parquet")
    w, s, e, n = item.bbox
    assert -8 < w < e < 2 and 49 < s < n < 61
    # plain tables fall back to the LAD extent from lad_index
    assert cat.get_item("epc-E1", recursive=True).bbox is not None
    assert stac.validate_stac(root / "stac")["missing_assets"] == []


def test_validates_with_pystac(rel):
    res = stac.validate_stac(rel[0] / "stac")
    assert "invalid" not in res  # schema fetch failing offline is tolerated, a real violation is not
    if res["validation_error"] is None:
        assert res["validated"] > 20


def test_national_assets_and_manifest(rel):
    root, _ = rel
    P.write_national(root, ("uprn",), threads=1)
    stac.build_stac(root, validate=False)
    cat = pystac.Catalog.from_file(str(root / "stac" / "catalog.json"))
    c = cat.get_child("uprn")
    assert c.assets["national_0000"].get_absolute_href().endswith("national/uprn/part-0000.parquet")
    assert json.loads((root / "stac" / "catalog.json").read_text())["type"] == "Catalog"
