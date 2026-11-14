"""Tests for uprn_resolver.address on synthetic data."""

import datetime as dt

import duckdb
import pytest

from uprn_resolver import address, publish

COLS = ["uprn", "certificate_number", "lodgement_date", "address1", "address2", "address3", "address",
        "posttown", "postcode"]  # fmt: skip


def _write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute(f"CREATE TABLE t ({', '.join(c + ' VARCHAR' for c in COLS)})")
    for r in rows:
        con.execute(f"INSERT INTO t VALUES ({', '.join('?' * len(COLS))})", r)
    con.execute(f"COPY t TO '{path}' (FORMAT parquet)")


@pytest.fixture
def env(tmp_path):
    st = tmp_path / "staged" / "E1"
    st.mkdir(parents=True)
    con = duckdb.connect()
    con.execute(
        "COPY (SELECT * FROM (VALUES (1::BIGINT,'SW1A 1AA'),(2,'SW1A 1AB'),(3,'SW1A 1AC'),(4,NULL),(5,'SW1A 1AD'),(6,'SW1A 2AA'),(7,'SW1A 2AB'),(8,'SW1A 2AC'))"
        f" t(uprn, postcode)) TO '{st / 'onsud.parquet'}' (FORMAT parquet)"
    )
    con.execute(
        "COPY (SELECT * FROM (VALUES (1::BIGINT,10::BIGINT,'High St','Town'),(2,10,'High St',NULL),"
        "(3,11,'Low Rd',NULL),(4,12,'Mid Rd',NULL)) t(uprn, usrn, street_name, locality))"
        f" TO '{st / 'streets.parquet'}' (FORMAT parquet)"
    )
    pq = tmp_path / "parquet" / "epc_ew"
    future = f"{dt.date.today().year + 2}-01-01"
    _write(
        pq / "domestic" / "certificates-2020.parquet",
        [
            ("1", "A", "2015-01-01", "1 Old Rd", None, None, None, "Oldtown", "SW1A 1AA"),
            ("1", "B", "2020-05-05", "1 New Rd", "Flat", None, None, "Newtown", "sw1a1aa"),
            ("2", "C", "2019-01-01", "2 Rd", None, None, None, "T", "ZZ9 9ZZ"),  # mismatch
            ("5", "D", future, "bad date", None, None, None, "T", "SW1A 1AD"),
            ("5", "E", "2010-01-01", "5 Good", None, None, None, "T", "SW1A 1AD"),
        ],
    )
    _write(
        pq / "non_domestic" / "certificates-2020.parquet",
        [
            ("2", "F", "2019-01-01", "Tie Shop", None, None, None, "T", "ZZ9 9ZZ"),  # tie -> domestic wins
            ("4", "G", "2018-01-01", "Unit 4", None, None, None, "T", None),
        ],
    )
    sc = pq.parent / "epc_scot" / "domestic"
    sc.mkdir(parents=True)
    duckdb.sql(
        "COPY (SELECT '3' AS OSG_REFERENCE_NUMBER, '3 Scot Rd' AS ADDRESS1, 'Glasgow' AS POSTTOWN, "
        f"'SW1A 1AC' AS POSTCODE, '2021-03-03' AS LODGEMENT_DATE) TO '{sc / 's_dom.parquet'}' (FORMAT parquet)"
    )
    ppd = tmp_path / "out" / "lad=E1"
    ppd.mkdir(parents=True)
    duckdb.sql(
        "COPY (SELECT * FROM (VALUES "
        "(1::BIGINT,'T0','2022-01-01'::DATE,'hmlr_lookup','authoritative','FLAT 1','9','PPD RD',NULL,'PPDTOWN','SW1A 1AA'),"
        "(6,'T1','2020-01-01','address_match','high','FLAT 2','12','HIGH STREET','OLDLOC','LONDON','SW1A 2AA'),"
        "(6,'T2','2023-02-02','hmlr_lookup','authoritative',NULL,'12','HIGH STREET','OLDLOC','LONDON','SW1A 2AA'),"
        "(7,'T3','2021-01-01','address_match','medium',NULL,'5','LOW STREET',NULL,'LONDON','SW1A 2AB'),"
        "(8,'T4','2021-01-01','address_match','high',NULL,'ROSE COTTAGE','LANE END',NULL,'LONDON','SW1B 9ZZ')"
        ") t(uprn, transaction_id, date, match_method, match_confidence, saon, paon, street, locality, town_city,"
        f" postcode)) TO '{ppd / 'ppd_local.parquet'}' (FORMAT parquet)"
    )
    return tmp_path


def _rows(env):
    res = address.address_local("E1", env / "staged", env / "out", env / "parquet")
    f = env / "out" / "lad=E1" / "address_local.parquet"
    con = duckdb.connect()
    cur = con.execute(f"SELECT * FROM '{f}'")
    names = [d[0] for d in cur.description]
    rows = {t[0]: dict(zip(names, t, strict=True)) for t in cur.fetchall()}
    return res, rows, f


def test_latest_and_ties(env):
    res, rows, _ = _rows(env)
    assert res["uprns"] == 8
    assert rows[1]["address_line1"] == "1 New Rd"
    assert rows[1]["single_line_address"] == "1 New Rd, Flat, Newtown, sw1a1aa"
    assert rows[2]["address_source"] == "epc_domestic"
    assert rows[5]["address_line1"] == "5 Good"  # future-dated cert ignored in favour of valid one


def test_fallback_and_licence(env):
    _, rows, _ = _rows(env)
    assert rows[3]["address_source"] == "epc_scot_domestic"  # Scottish as-is parquet (aliased columns)
    assert rows[3]["address_line1"] == "3 Scot Rd"
    assert rows[3]["licence"] == "EPC-restricted"
    assert rows[1]["street_name_open"] == "High St"
    assert rows[5]["address_source"] == "epc_domestic"
    assert rows[4]["address_source"] == "epc_non_domestic" and rows[4]["licence"] == "EPC-restricted"
    assert rows[1]["licence"] == "EPC-restricted"


def test_postcode_mismatch(env):
    _, rows, _ = _rows(env)
    assert rows[1]["postcode_mismatch"] is False  # normalised comparison
    assert rows[2]["postcode_mismatch"] is True
    assert rows[3]["postcode_mismatch"] is False


def test_none_source_and_sidecar_and_publish_rejects(env, tmp_path):
    con = duckdb.connect()
    con.execute(
        f"COPY (SELECT 9::BIGINT uprn, 'AB1 1AB' postcode) TO '{env / 'staged' / 'E2' / 'onsud.parquet'}'"
        if (env / "staged" / "E2").mkdir() is None
        else ""
    )
    con.execute(
        f"COPY (SELECT 1::BIGINT uprn, 1::BIGINT usrn, 'x' street_name, 'y' locality WHERE false) "
        f"TO '{env / 'staged' / 'E2' / 'streets.parquet'}'"
    )
    res = address.address_local_all(env / "staged", env / "out", env / "parquet")
    assert set(res["built"]) == {"E1", "E2"}
    f2 = env / "out" / "lad=E2" / "address_local.parquet"
    assert con.execute(f"SELECT address_source FROM '{f2}'").fetchone()[0] == "none"
    assert (env / "out" / "lad=E2" / "address_local.LICENCE.txt").exists()
    assert address.address_local_all(env / "staged", env / "out", env / "parquet")["built"] == []
    assert publish.validate_parquet(env / "out" / "lad=E1" / "address_local.parquet", require_geo=False)


def test_ppd_fallback(env):
    _, rows, _ = _rows(env)
    # EPC wins over PPD
    assert rows[1]["address_source"] == "epc_domestic" and rows[1]["address_line1"] == "1 New Rd"
    # latest qualifying PPD row; numbered paon joins the street, saon leads
    assert rows[6]["address_source"] == "ppd" and rows[6]["licence"] == "PPD-restricted"
    assert rows[6]["single_line_address"] == "12 HIGH STREET, OLDLOC, LONDON, SW1A 2AA"
    assert rows[6]["address_cert_date"] == dt.date(2023, 2, 2)
    assert rows[6]["postcode_mismatch"] is False
    # low/medium address_match is not used
    assert rows[7]["address_source"] == "none" and rows[7]["licence"] == "open-partial"
    # house name stays a separate element; postcode disagreeing with ONSUD is flagged
    assert rows[8]["single_line_address"] == "ROSE COTTAGE, LANE END, LONDON, SW1B 9ZZ"
    assert rows[8]["postcode_mismatch"] is True


def test_no_ppd_file(env):
    (env / "out" / "lad=E1" / "ppd_local.parquet").unlink()
    _, rows, _ = _rows(env)
    assert rows[6]["address_source"] == "none" and rows[1]["address_source"] == "epc_domestic"
    assert "HM Land Registry" in address.LICENCE_TEXT and "Royal Mail" in address.LICENCE_TEXT
