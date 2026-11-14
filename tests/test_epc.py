import json
from pathlib import Path

import duckdb
import pytest

from uprn_resolver.epc import (
    CORE_COLS,
    _ew_type,
    convert_epc_csv,
    is_denied,
    snake,
    stage_epc,
    validate_columns,
)

EW_DOM = """LMK_KEY,ADDRESS1,ADDRESS2,ADDRESS3,ADDRESS,POSTCODE,BUILDING_REFERENCE_NUMBER,LOCAL_AUTHORITY,LOCAL_AUTHORITY_LABEL,CONSTITUENCY,COUNTY,LODGEMENT_DATE,LODGEMENT_DATETIME,POSTTOWN,UPRN,UPRN_SOURCE,PROPERTY_TYPE,BUILT_FORM,TOTAL_FLOOR_AREA,CONSTRUCTION_AGE_BAND,CURRENT_ENERGY_RATING,CURRENT_ENERGY_EFFICIENCY,FLAT_TOP_STOREY,MAINS_GAS_FLAG
a1,1 High St,Flat,x,1 High St Flat,N1 1AA,111,E09000001,Islington,E14000001,Greater London,2015-01-01,2015-01-01 10:00:00,LONDON,100,Energy Assessor,House,Mid-Terrace,80.5,1900-1929,D,60,00,Y
a2,1 High St,Flat,x,1 High St Flat,N1 1AA,112,E09000001,Islington,E14000001,Greater London,2020-06-01,2020-06-01 10:00:00,LONDON,100,Energy Assessor,House,End-Terrace,81,1900-1929,C,72,00,N
a3,2 High St,,x,2 High St,N1 1AA,113,E09000001,Islington,E14000001,Greater London,2019-01-01,2019-01-01 10:00:00,LONDON,,,Flat,,40,,E,40,01,
a4,3 High St,,x,3 High St,N1 1AB,114,E09000001,Islington,E14000001,Greater London,2018-01-01,2018-01-01 10:00:00,LONDON,200,Address Matched,Flat,Detached,50,2000,B,85,00,Y
"""
EW_ND = """LMK_KEY,ADDRESS,POSTCODE,UPRN,PROPERTY_TYPE,FLOOR_AREA,ASSET_RATING_BAND,LODGEMENT_DATETIME,SPECIAL_ENERGY_USES
n1,Unit 1,N1 1AA,100,Retail,120,B,2020-06-01 10:00:00,Kiln at 5 Mill Lane
n2,Unit 2,N1 1AA,300,Office,50,C,2017-01-01 10:00:00,
"""
EW_DEC = """LMK_KEY,ADDRESS1,POSTCODE,POSTTOWN,UPRN,BUILDING_REFERENCE_NUMBER,OPERATIONAL_RATING_BAND,CURRENT_OPERATIONAL_RATING,TOTAL_FLOOR_AREA,LODGEMENT_DATETIME
d1,Town Hall,N1 1AA,LONDON,500,9001,D,105,2000.5,2021-03-01 10:00:00
"""
SCOT = """HISTORIC,BUILDING_REFERENCE_NUMBER,OSG_REFERENCE_NUMBER,ADDRESS1,ADDRESS2,ADDRESS3,POSTCODE,INSPECTION_DATE,LODGEMENT_DATE,TOTAL_FLOOR_AREA,CURRENT_ENERGY_RATING,3_YR_ENERGY_COST_CURRENT,WALL_ENERGY_EFF,IMPROVEMENTS,ADDENDUM_TEXT,DATA_ZONE,CONSTRUCTION_AGE_BAND,BUILT_FORM,PROPERTY_TYPE,CREATED_AT,REPORT_REFERENCE_NUMBER
HIST,Property_UPRN,OSG_UPRN,Addr,Addr2,POST_TOWN,Postcode,Date of Assessment,Date of Certificate,Total floor area,Band,Costs,Eff,Improvements,Addendum,Data Zone,Age,Built Form,Property Type,Lodgement Date,RRN
N,1,400,5 Road,,EDINBURGH,EH1 1AA,2026-03-24,2026-03-27,46,C,2430.0000,Poor,Floor insulation at 5 Road,Text,S01002243 (Bonnington),before 1919,,Flat,27/03/2026 09:47:24,r1
N,2,401,6 Road,,EDINBURGH,EH1 1AA,2026-03-24,2099-01-01,46,C,100.0000,Good,,,S01002243 (Bonnington),before 1919,,Flat,27/03/2026 09:47:24,r2
N,3,401,6 Road,,EDINBURGH,EH1 1AA,2004-03-24,1999-01-01,46,E,100.0000,Good,,,S01002243 (Bonnington),before 1919,,Flat,27/03/2026 09:47:24,r3
N,4,401,6 Road,,EDINBURGH,EH1 1AA,2012-03-24,2012-02-01,46,D,100.0000,Good,,,S01002243 (Bonnington),before 1919,,Flat,27/03/2026 09:47:24,r4
"""


def _setup(tmp_path: Path):
    pq = tmp_path / "parquet"
    for name, text, t, c in [
        ("ew_dom", EW_DOM, "domestic", "EW"),
        ("ew_nd", EW_ND, "non-domestic", "EW"),
        ("ew_dec", EW_DEC, "dec", "EW"),
        ("s_dom", SCOT, "domestic", "S"),
    ]:
        f = tmp_path / f"{name}.csv"
        f.write_text(text)
        convert_epc_csv(f, pq, name, t, c)
    return pq


def _rows(path):
    con = duckdb.connect()
    cur = con.execute(f"SELECT * FROM read_parquet('{path}')")
    cols = [d[0] for d in cur.description]
    return cols, [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


def test_keeps_all_non_address_columns_and_drops_address(tmp_path):
    pq = _setup(tmp_path)
    cols, rows = _rows(pq / "epc" / "ew_dom.parquet")
    assert tuple(cols[: len(CORE_COLS)]) == CORE_COLS
    for keep in ("building_reference_number", "local_authority", "local_authority_label", "constituency",
                 "current_energy_efficiency", "flat_top_storey", "mains_gas_flag", "lodgement_datetime"):  # fmt: skip
        assert keep in cols
    for drop in ("address", "address1", "address2", "address3", "postcode", "posttown", "county"):
        assert drop not in cols
    assert not any(is_denied(c) for c in cols)
    assert sorted(r["uprn"] for r in rows) == [100, 100, 200]
    assert {r["flat_top_storey"] for r in rows} == {"00"}  # leading zeros -> text
    assert all(isinstance(r["current_energy_efficiency"], int) for r in rows)
    assert isinstance(rows[0]["total_floor_area"], float)
    n_in, n_out = convert_epc_csv(tmp_path / "ew_dom.csv", pq, "ew_dom", "domestic", "EW")
    assert (n_in, n_out) == (4, 3)
    m = json.loads((pq / "manifest.json").read_text())["converted"]["epc/ew_dom.parquet"]
    assert m["rows"] == 3 and "address1" in m["dropped_columns"] and "postcode" in m["dropped_columns"]


def test_nd_dec_and_freetext_dropped(tmp_path):
    pq = _setup(tmp_path)
    cols, rows = _rows(pq / "epc" / "ew_nd.parquet")
    assert "special_energy_uses" not in cols and "address" not in cols
    assert {r["current_energy_rating"] for r in rows} == {"B", "C"} and rows[0]["total_floor_area"] == 120.0
    assert rows[0]["lodgement_date"].isoformat() == "2020-06-01"
    cols, rows = _rows(pq / "epc" / "ew_dec.parquet")
    assert rows[0]["certificate_type"] == "dec" and rows[0]["current_energy_rating"] == "D"
    assert rows[0]["current_operational_rating"] == 105 and rows[0]["building_reference_number"] == "9001"
    assert _ew_type("all-display-certificates.zip") == "dec"
    assert _ew_type("all-non-domestic-certificates.zip") == "non-domestic"
    assert _ew_type("all-domestic-certificates.zip") == "domestic"


def test_scotland_uses_osg_skips_description_row_and_bad_dates(tmp_path):
    pq = _setup(tmp_path)
    cols, rows = _rows(pq / "epc" / "s_dom.parquet")
    by = {r["lmk_key"]: r for r in rows}
    assert set(by) == {"r1", "r2", "r3", "r4"}
    assert by["r1"]["uprn"] == 400 and by["r1"]["country"] == "S" and by["r1"]["uprn_source"] == "OSG_REFERENCE_NUMBER"
    assert by["r1"]["n3_yr_energy_cost_current"] == 2430.0
    assert by["r2"]["lodgement_date"] is None and by["r3"]["lodgement_date"] is None  # future / pre-2007
    assert by["r4"]["lodgement_date"].isoformat() == "2012-02-01"
    assert by["r1"]["created_at"].year == 2026 and by["r1"]["data_zone"].startswith("S01002243")
    for gone in ("osg_reference_number", "improvements", "addendum_text", "address1", "address3", "postcode"):
        assert gone not in cols


def test_validate_columns_rejects_address_names():
    validate_columns(["uprn", "local_authority_label", "building_reference_number", "constituency_label"])
    for bad in ("address", "address1", "postcode", "post_town", "posttown", "county", "building_name", "locality",
                "improvements", "addendum_text"):  # fmt: skip
        with pytest.raises(ValueError):
            validate_columns(["uprn", bad])
    assert snake("\ufeffHISTORIC") == "historic" and snake("3_YR_X") == "n3_yr_x"


def test_convert_fails_if_denylisted_column_would_be_written(tmp_path, monkeypatch):
    import uprn_resolver.epc as epc

    monkeypatch.setattr(epc, "is_denied", lambda n: False)  # simulate a denylist hole -> validation still fires
    f = tmp_path / "x.csv"
    f.write_text("LMK_KEY,UPRN,POSTCODE\nk,1,AB1\n")
    with pytest.raises(ValueError):
        convert_epc_csv(f, tmp_path / "parquet", "x", "domestic", "EW")


def _ew_as_is(tmp_path: Path) -> Path:
    """As-is E&W conversions (all VARCHAR, with address columns) under parquet/epc_ew/, plus a stale ew_dec copy."""
    pq = tmp_path / "parquet"
    for sub in ("domestic", "non_domestic", "display"):
        (pq / "epc_ew" / sub).mkdir(parents=True)
    (pq / "epc").mkdir(parents=True, exist_ok=True)

    def w(rel, header, rows):
        f = tmp_path / "x.csv"
        f.write_text(header + "\n" + "\n".join(rows) + "\n")
        duckdb.execute(
            f"COPY (SELECT * FROM read_csv('{f}', header=true, all_varchar=true)) TO '{pq / rel}' (FORMAT parquet)"
        )

    w("epc_ew/domestic/certificates-2020.parquet",
      "certificate_number,address1,address,postcode,posttown,built_form,property_type,total_floor_area,"
      "current_energy_rating,current_energy_efficiency,lodgement_date,lodgement_datetime,country,uprn,uprn_source",
      ["a1,1 High St,1 High St,N1 1AA,LONDON,Mid-Terrace,House,80.5,D,60,2015-01-01,2015-01-01 10:00:00,England,100,EA",
       "a2,1 High St,1 High St,N1 1AA,LONDON,End-Terrace,House,81,C,72,2020-06-01,2020-06-01 10:00:00,England,100,EA",
       "a3,2 High St,2 High St,N1 1AA,LONDON,,Flat,40,E,40,2019-01-01,2019-01-01 10:00:00,England,,",
       "a4,3 High St,3 High St,N1 1AB,LONDON,Detached,Flat,50,B,85,2018-01-01,2018-01-01 10:00:00,England,200,EA",
       "a5,9 High St,9 High St,N1 1AB,LONDON,Detached,Flat,50,B,85,2018-01-01,2018-01-01 10:00:00,England,999,EA"])  # fmt: skip
    w("epc_ew/non_domestic/certificates-2020.parquet",
      "certificate_number,address,postcode,uprn,property_type,floor_area,asset_rating_band,lodgement_date,"
      "lodgement_datetime,special_energy_uses",
      ["n1,Unit 1,N1 1AA,100,Retail,120,B,2020-06-01,2020-06-01 10:00:00,Kiln at 5 Mill Lane",
       "n2,Unit 2,N1 1AA,300,Office,50,C,2017-01-01,2017-01-01 10:00:00,",
       "n3,Unit 3,N1 1AA,300,Office,50,C,2003-01-01,2003-01-01 10:00:00,"])  # fmt: skip
    w("epc_ew/display/certificates-2020.parquet",
      "certificate_number,address1,postcode,uprn,operational_rating_band,current_operational_rating,"
      "total_floor_area,lodgement_date,lodgement_datetime",
      ["d1,Town Hall,N1 1AA,500,D,105,2000.5,2021-03-01,2021-03-01 10:00:00"])  # fmt: skip
    # stale filtered copy (superseded): must be ignored, not double counted
    w("epc/ew_dec_2021.parquet", "uprn,lmk_key,certificate_type", ["500,d1,dec"])
    return pq


def test_stage_ew_as_is_latest_and_domestic_preferred(tmp_path):
    _ew_as_is(tmp_path)
    raw, staged = tmp_path / "raw", tmp_path / "staged"
    (staged / "E1").mkdir(parents=True)
    duckdb.execute(
        f"COPY (SELECT * FROM (VALUES (100::BIGINT),(200),(300),(500),(1000)) t(uprn)) "
        f"TO '{staged / 'E1' / 'onsud.parquet'}' (FORMAT parquet)"
    )
    stage_epc("E1", raw, staged)
    cols, rows = _rows(staged / "E1" / "epc.parquet")
    assert not any(is_denied(c) for c in cols)
    for gone in ("address", "address1", "postcode", "posttown", "special_energy_uses"):
        assert gone not in cols
    by = {r["uprn"]: r for r in rows}
    assert set(by) == {100, 200, 300, 500}  # 1000 has no cert; 999 isn't in the LAD
    assert by[100]["lmk_key"] == "a2" and by[100]["certificate_type"] == "domestic"  # date tie: domestic wins
    assert by[100]["epc_count"] == 3 and by[100]["country"] == "EW"
    assert by[100]["lodgement_date"].isoformat() == "2020-06-01" and by[100]["total_floor_area"] == 81.0
    assert by[100]["built_form"] == "End-Terrace" and by[100]["current_energy_rating"] == "C"
    assert by[100]["floor_area"] == 120.0 or by[100]["floor_area"] is None  # ND-only column unioned by name
    assert by[200]["certificate_type"] == "domestic" and by[200]["built_form"] == "Detached"
    assert by[200]["current_energy_efficiency"] == 85 and by[200]["total_floor_area"] == 50.0
    assert by[300]["epc_count"] == 2 and by[300]["lmk_key"] == "n2"  # pre-2007 cert counted, sorts last
    assert by[500]["certificate_type"] == "dec" and by[500]["epc_count"] == 1  # ew_dec ignored
    assert by[500]["current_energy_rating"] == "D" and by[500]["current_operational_rating"] == 105


def test_stage_latest_and_domestic_preferred(tmp_path):
    _setup(tmp_path)
    raw, staged = tmp_path / "raw", tmp_path / "staged"
    (staged / "E1").mkdir(parents=True)
    duckdb.execute(
        f"COPY (SELECT * FROM (VALUES (400::BIGINT),(401),(999)) t(uprn)) TO '{staged / 'E1' / 'onsud.parquet'}' (FORMAT parquet)"
    )
    stage_epc("E1", raw, staged)
    _, rows = _rows(staged / "E1" / "epc.parquet")
    by = {r["uprn"]: r for r in rows}
    assert set(by) == {400, 401}  # Scotland s_*.parquet only
    assert by[401]["lmk_key"] == "r4" and by[401]["epc_count"] == 3 and by[401]["country"] == "S"


def test_stage_noop_without_epc(tmp_path, caplog):
    staged = tmp_path / "staged"
    (staged / "E1").mkdir(parents=True)
    duckdb.execute(f"COPY (SELECT 1::BIGINT uprn) TO '{staged / 'E1' / 'onsud.parquet'}' (FORMAT parquet)")
    stage_epc("E1", tmp_path / "raw", staged)
    assert not (staged / "E1" / "epc.parquet").exists()


def test_convert_ew_bulk_all_entries_as_is(tmp_path):
    import zipfile

    import duckdb

    from uprn_resolver.epc import convert_ew_bulk

    z = tmp_path / "non-domestic-csv.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("certificates-2024.csv", 'certificate_number,address1,uprn\n"A-1","1 High St, X",100\nA-2,,\n')
        zf.writestr("recommendations-2024.csv", 'certificate_number,text\nA-1,"multi\nline"\n')
    out = tmp_path / "parquet" / "epc_ew"
    res = convert_ew_bulk(z, out)
    assert {r["file"]: r["rows"] for r in res} == {"certificates-2024.parquet": 2, "recommendations-2024.parquet": 1}
    assert not z.exists()
    df = duckdb.sql(f"select * from '{out}/non_domestic/certificates-2024.parquet' order by 1").fetchall()
    assert df[0] == ("A-1", "1 High St, X", "100")
    assert not (out / ".work").exists()
    import json

    m = json.loads((tmp_path / "parquet" / "manifest.json").read_text())["converted"]
    assert "epc_ew/non_domestic/certificates-2024.parquet" in m


def test_convert_scot_bulk_as_is_drops_description_row(tmp_path, monkeypatch):
    import subprocess

    import duckdb

    from uprn_resolver import epc

    src = tmp_path / "src"
    src.mkdir()
    (src / "d1.csv").write_text(
        "OSG_REFERENCE_NUMBER,ADDRESS1,POSTCODE\n"
        "Unique property ref,First line of address,Postcode\n"
        '400,"1 High St, X",EH1 1AA\n,2 Low St,EH1 1AB\n'
    )
    arch = tmp_path / "raw" / "scot_domestic_historic.7z"
    monkeypatch.setattr(epc, "SCOT_ARCHIVES", {arch.name: ("http://x/y", "domestic")})
    monkeypatch.setattr(
        epc, "_fetch", lambda url, dest: subprocess.run(["tar", "-cf", str(dest), "-C", str(src), "d1.csv"], check=True)
    )
    out = tmp_path / "parquet" / "epc_scot"
    res = epc.convert_scot_bulk(tmp_path / "raw", out, min_free_gb=0)
    assert [(r["type"], r["file"], r["rows"], r["rows_in_csv"]) for r in res] == [("domestic", "d1.parquet", 2, 3)]
    rows = duckdb.sql(f"select * from '{out}/domestic/d1.parquet' order by 1 nulls last").fetchall()
    assert rows == [("400", "1 High St, X", "EH1 1AA"), (None, "2 Low St", "EH1 1AB")]
    assert not arch.exists() and not (out / ".work").exists()
    m = json.loads((tmp_path / "parquet" / "manifest.json").read_text())["converted"]
    assert m["epc_scot/domestic/d1.parquet"]["description_rows_dropped"] == 1
