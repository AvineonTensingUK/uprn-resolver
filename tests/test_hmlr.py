from pathlib import Path

import duckdb

from uprn_resolver import hmlr


def _setup(tmp_path: Path, with_inspire=True):
    raw, staged = tmp_path / "raw", tmp_path / "staged"
    (raw / "hmlr").mkdir(parents=True)
    (staged / "L1").mkdir(parents=True)
    duckdb.execute(
        f"COPY (SELECT * FROM (VALUES (100::BIGINT),(200::BIGINT)) v(uprn)) TO '{staged / 'L1' / 'onsud.parquet'}' (FORMAT PARQUET)"
    )
    (raw / "hmlr" / "pp-uprn-lookup-aug-2026.csv").write_text(
        '"{aaaa0000-0000-0000-0000-000000000001}","100"\n'
        '"{AAAA0000-0000-0000-0000-000000000002}","999"\n'
        '"AAAA0000-0000-0000-0000-000000000003","200"\n'
    )
    (raw / "hmlr" / "pp-uprn-lookup-sep-2026.csv").write_text(
        '"{AAAA0000-0000-0000-0000-000000000001}","100"\n"{AAAA0000-0000-0000-0000-000000000004}","200"\n'
    )
    if with_inspire:
        (raw / "hmlr" / "pp-inspire-id-lookup-aug-2026.csv").write_text(
            '"{AAAA0000-0000-0000-0000-000000000001}","11"\n'
            '"{AAAA0000-0000-0000-0000-000000000002}","22"\n'
            '"{AAAA0000-0000-0000-0000-000000000003}","33"\n'
            '"{AAAA0000-0000-0000-0000-000000000003}","34"\n'
        )
    return raw, staged


def test_stage_normalises_and_filters(tmp_path):
    raw, staged = _setup(tmp_path)
    hmlr.stage_hmlr("L1", raw, staged)
    ppd = duckdb.sql(f"select * from '{staged / 'L1' / 'hmlr_ppd_uprn.parquet'}' order by 1,2").fetchall()
    assert ppd == [
        ("{AAAA0000-0000-0000-0000-000000000001}", 100),  # deduped across months/case
        ("{AAAA0000-0000-0000-0000-000000000003}", 200),  # braces added
        ("{AAAA0000-0000-0000-0000-000000000004}", 200),
    ]
    ins = duckdb.sql(f"select * from '{staged / 'L1' / 'hmlr_inspire.parquet'}' order by 1,2").fetchall()
    assert ins == [
        ("{AAAA0000-0000-0000-0000-000000000001}", "11"),
        ("{AAAA0000-0000-0000-0000-000000000003}", "33"),
        ("{AAAA0000-0000-0000-0000-000000000003}", "34"),
    ]


def test_missing_inspire_is_noop(tmp_path, caplog):
    raw, staged = _setup(tmp_path, with_inspire=False)
    hmlr.stage_hmlr("L1", raw, staged)
    assert (staged / "L1" / "hmlr_ppd_uprn.parquet").exists()
    assert not (staged / "L1" / "hmlr_inspire.parquet").exists()


def test_months():
    import datetime as dt

    assert list(hmlr._months(dt.date(2026, 8, 30))) == [(2026, 8)]
    assert list(hmlr._months(dt.date(2027, 1, 2)))[-1] == (2027, 1)
    assert len(list(hmlr._months(dt.date(2027, 1, 2)))) == 6


def test_archive_downloads_new_and_reports_gaps(tmp_path, monkeypatch):
    import datetime as dt

    raw = tmp_path / "raw"
    pq = tmp_path / "parquet" / "hmlr"
    pq.mkdir(parents=True)
    duckdb.execute(
        f"COPY (SELECT '{{A}}' AS transaction_id, '1' AS value) TO '{pq / 'pp-uprn-lookup-aug-2026.parquet'}'"
    )
    served = {"pp-uprn-lookup-sep-2026.csv": '"{AAAA0000-0000-0000-0000-000000000001}","100"\n'}

    def fake_fetch(url, dest):
        name = url.rsplit("/", 1)[1]
        if name not in served:
            return False
        dest.write_text(served[name])
        return True

    monkeypatch.setattr(hmlr, "_fetch", fake_fetch)
    res = hmlr.archive_hmlr(raw, today=dt.date(2026, 9, 15))
    assert res["new"] == ["pp-uprn-lookup-sep-2026.parquet"]
    assert (pq / "pp-uprn-lookup-sep-2026.parquet").exists()
    assert not list((raw / "hmlr").glob("*.csv"))  # converted, CSV removed
    # inspire files never archived -> reported as missing
    assert "pp-inspire-id-lookup-aug-2026" in res["missing"]
    assert "pp-uprn-lookup-aug-2026" not in res["missing"]
