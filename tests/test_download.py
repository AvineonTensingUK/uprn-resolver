import json
import sys
import types
import zipfile
from pathlib import Path

import pytest

from uprn_resolver import download as dl


def _make_zip(path: Path, members: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        for k, v in members.items():
            z.writestr(k, v)


@pytest.fixture
def fake_convert(monkeypatch):
    calls = []
    mod = types.ModuleType("uprn_resolver.convert")

    def convert(dataset, src, parquet_dir):
        calls.append((dataset, Path(src).name, Path(src).exists()))
        return 2

    mod.convert = convert
    monkeypatch.setitem(sys.modules, "uprn_resolver.convert", mod)
    monkeypatch.setattr(dl, "_wait_for_space", lambda *a, **k: None)
    return calls


def test_delete_after_convert(tmp_path, monkeypatch, fake_convert):
    raw = tmp_path / "raw"
    src_zip = tmp_path / "src.zip"
    _make_zip(src_zip, {"data/tq.csv": "a\n1\n2\n"})

    def fake_stream(url, dest, size=None, md5=None):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(src_zip.read_bytes())
        return dest

    monkeypatch.setattr(dl, "_stream", fake_stream)
    monkeypatch.setattr(
        dl,
        "_os_listing",
        lambda p: [{"format": "CSV", "area": "GB", "fileName": "uprn.zip", "url": "http://x", "size": 1}],
    )
    dl.download(["open_uprn"], raw)
    assert fake_convert == [("open_uprn", "tq.csv", True)]
    assert list((raw / "open_uprn").iterdir()) == []  # zip and extracted files gone
    rec = json.loads((tmp_path / "parquet" / "manifest.json").read_text())
    assert rec["downloads"]["uprn.zip"]["rows"] == 2


def test_skip_via_manifest(tmp_path, monkeypatch, fake_convert):
    raw = tmp_path / "raw"
    pq = tmp_path / "parquet"
    pq.mkdir()
    (pq / "manifest.json").write_text(json.dumps({"downloads": {"uprn.zip": {"dataset": "open_uprn", "md5": "abc"}}}))
    monkeypatch.setattr(dl, "_stream", lambda *a, **k: pytest.fail("should not download"))
    monkeypatch.setattr(
        dl, "_os_listing", lambda p: [{"format": "CSV", "area": "GB", "fileName": "uprn.zip", "url": "http://x"}]
    )
    dl.download(["open_uprn"], raw)
    assert fake_convert == []


def test_skip_via_md5_and_extracted_marker(tmp_path):
    pq = tmp_path / "parquet"
    pq.mkdir()
    (pq / "manifest.json").write_text(
        json.dumps({"downloads": {"x.zip": {"md5": "ABC"}}, "converted": {"open_toid/tq.parquet": {}}})
    )
    assert dl._already_converted(pq, "other.zip", "abc")
    assert dl._already_converted(pq, "o.zip", None, "open_toid/tq.parquet")
    assert not dl._already_converted(pq, "other.zip", "def")


def test_failed_convert_keeps_zip(tmp_path, monkeypatch, fake_convert):
    sys.modules["uprn_resolver.convert"].convert = lambda *a: 0
    z = tmp_path / "raw" / "onsud" / "o.zip"
    _make_zip(z, {"a.csv": "x\n"})
    with pytest.raises(RuntimeError):
        dl._process("onsud", z, tmp_path / "parquet", 1, None)
    assert z.exists()
    assert not any(p.name.startswith(".x_") for p in z.parent.iterdir())


def test_tq_first_and_areas(monkeypatch):
    listing = [{"format": "CSV", "area": a, "fileName": f"{a}.zip"} for a in ("SU", "TQ", "HP", "GB")]
    out = dl._select("open_toid", listing, ["all"])
    assert out[0]["area"] == "TQ" and "GB" not in [d["area"] for d in out]
    assert [d["area"] for d in dl._select("open_toid", listing, ["tq"])] == ["TQ"]


def test_unsafe_zip_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "_wait_for_space", lambda *a, **k: None)
    z = tmp_path / "bad.zip"
    _make_zip(z, {"../evil.txt": "x"})
    with pytest.raises(OSError):
        dl._unzip(z, tmp_path / "t")


def test_openmap_building_layer_only(tmp_path):
    files = [tmp_path / n for n in ("TQ_Building.shp", "TQ_ImportantBuilding.shp", "TQ_Road.shp")]
    assert [f.name for f in dl._sources("openmap_local", files)] == ["TQ_Building.shp"]
