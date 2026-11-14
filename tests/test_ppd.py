"""Tests for uprn_resolver.ppd on synthetic data (no network, no real address text)."""

import datetime as dt

import duckdb
import pytest

from uprn_resolver import publish
from uprn_resolver.ppd import (
    _csv_chunk_to_parquet,
    address_parts,
    norm_address,
    norm_postcode,
    norm_street,
    ppd_all,
    ppd_lad,
)

LAD = "E1"
G = "{%s}"


def _tid(n: int) -> str:
    return f"{{00000000-0000-0000-0000-{n:012d}}}"


def test_normalisation():
    assert norm_postcode("n1 9aa") == "N19AA"
    assert norm_postcode(None) is None
    assert norm_address("12-14, Foo Rd.") == "12~14 FOO RD"
    assert norm_address("12 to 14 Foo Road") == "12~14 FOO ROAD"
    assert norm_street("Foo Rd") == "FOO ROAD"
    assert norm_street("Bar St.") == "BAR STREET"


@pytest.mark.parametrize(
    ("text", "flat", "house"),
    [
        ("12A Foo Road", None, "12A"),
        ("12-14 Foo Road", None, "12~14"),
        ("Flat 3 12 Foo Road", "3", "12"),
        ("Flat A, 12 Foo Road", "A", "12"),
        ("Apartment 12A Bar House 5 Foo Road", "12A", "5"),
        ("3 12 Foo Road", "3", "12"),
        ("First Floor Flat 269 Upper Street", "FIRST", "269"),
        ("Bar House Foo Road", None, None),
    ],
)
def test_address_parts(text, flat, house):
    assert address_parts(text) == {"flat": flat, "house": house}


def test_csv_chunk(tmp_path):
    csv = tmp_path / "c.csv"
    csv.write_text(
        '"{abc-1}","100","2020-01-02 00:00","N1 9AA","T","N","F","12","","FOO ROAD","","LONDON","X","Y","A","A"\n'
        '"{ABC-2}","200","2021-03-04 00:00","N1 9AB","F","Y","L","3","FLAT 1","BAR ST","","LONDON","X","Y","B","A"\n'
    )
    out = tmp_path / "c.parquet"
    assert _csv_chunk_to_parquet(csv, out) == (2, 2)
    rows = (
        duckdb.connect()
        .execute(f"SELECT transaction_id, price, date_of_transfer, saon FROM '{out}' ORDER BY 1")
        .fetchall()
    )
    assert rows == [("{ABC-1}", 100, dt.date(2020, 1, 2), None), ("{ABC-2}", 200, dt.date(2021, 3, 4), "FLAT 1")]


def _write(path, rows, cols):
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute(f"CREATE TABLE t ({', '.join(c + ' VARCHAR' for c in cols)})")
    for r in rows:
        con.execute(f"INSERT INTO t VALUES ({', '.join('?' * len(cols))})", r)
    con.execute(f"COPY t TO '{path}' (FORMAT parquet)")


PPD_COLS = ["transaction_id", "price", "date_of_transfer", "postcode", "property_type", "old_new", "duration",
            "paon", "saon", "street", "locality", "town_city", "district", "county", "ppd_category_type",
            "record_status"]  # fmt: skip


def _ppd_row(n, price, date, pc, paon, saon, street, status="A"):
    return [_tid(n), price, date, pc, "T", "N", "F", paon, saon, street, None, "LONDON", "D", "C", "A", status]


@pytest.fixture
def env(tmp_path):
    pq = tmp_path / "data" / "parquet"
    staged = tmp_path / "data" / "staged" / LAD
    out = tmp_path / "data" / "out"
    # UPRNs: 1 = 12 Foo Road, 2 = flat 1 / 12 Bar Road, 3 = flat 2 / 12 Bar Road, 4,5 = identical '7 Dup Road' (ambiguous),
    # 6 = 99 Baz Road (only the HMLR lookup knows it), 7 = 5 Wrong Road (lookup)
    onsud = [(1, "N1 9AA"), (2, "N1 9AB"), (3, "N1 9AB"), (4, "N1 9AC"), (5, "N1 9AC"), (6, "N1 9AD"), (7, "N1 9AE")]
    staged.mkdir(parents=True)
    con = duckdb.connect()
    con.execute("CREATE TABLE o (uprn BIGINT, lad VARCHAR, oa VARCHAR, lsoa VARCHAR, msoa VARCHAR, postcode VARCHAR)")
    for u, pc in onsud:
        con.execute("INSERT INTO o VALUES (?, ?, 'o', 'l', 'm', ?)", [u, LAD, pc])
    con.execute(f"COPY o TO '{staged / 'onsud.parquet'}' (FORMAT parquet)")
    ecols = [
        "uprn",
        "certificate_number",
        "lodgement_date",
        "address1",
        "address2",
        "address3",
        "address",
        "posttown",
        "postcode",
    ]
    _write(
        pq / "epc_ew" / "domestic" / "certificates-2024.parquet",
        [
            ["1", "c1", "2024-01-01", "12 Foo Road", None, None, None, "LONDON", "N1 9AA"],
            ["2", "c2", "2024-01-01", "Flat 1", "12 Bar Road", None, None, "LONDON", "N1 9AB"],
            ["3", "c3", "2024-01-01", "Flat 2", "12 Bar Road", None, None, "LONDON", "N1 9AB"],
            ["4", "c4", "2024-01-01", "7 Dup Road", None, None, None, "LONDON", "N1 9AC"],
            ["5", "c5", "2024-01-01", "7 Dup Road", None, None, None, "LONDON", "N1 9AC"],
            ["7", "c7", "2024-01-01", "5 Right Road", None, None, None, "LONDON", "N1 9AE"],
        ],
        ecols,
    )
    ppd_rows = [
        _ppd_row(1, 100000, "2001-01-01", "N1 9AA", "12", None, "FOO ROAD"),  # address match high
        _ppd_row(2, 200000, "2010-05-05", "N1 9AA", "12", None, "FOO RD"),  # address match high (street abbr)
        _ppd_row(3, 150000, "2005-01-01", "N1 9AB", "12", "FLAT 1", "BAR ROAD"),  # flat match
        _ppd_row(4, 160000, "2006-01-01", "N1 9AB", "12", None, "BAR ROAD"),  # no flat on PPD side -> must NOT match
        _ppd_row(5, 170000, "2007-01-01", "N1 9AC", "7", None, "DUP ROAD"),  # ambiguous -> no match
        _ppd_row(6, 300000, "2026-08-10", "N1 9AD", "99", None, "BAZ ROAD"),  # lookup only
        _ppd_row(7, 400000, "2026-08-11", "N1 9AE", "5", None, "RIGHT ROAD"),  # lookup + address match agree
        _ppd_row(8, 50000, "2000-01-01", "N1 9AA", "12", None, "FOO ROAD", status="D"),  # deleted: dropped
        _ppd_row(9, 180000, "2008-01-01", "N1 9AA", "12", None, "OTHER ROAD"),  # street disagrees -> low
    ]
    _write(pq / "ppd" / "ppd.parquet", ppd_rows, PPD_COLS)
    # the date/price columns are VARCHAR in the helper; cast so the production schema holds
    con = duckdb.connect()
    con.execute(
        f"""COPY (SELECT * REPLACE (CAST(price AS BIGINT) AS price, CAST(date_of_transfer AS DATE) AS date_of_transfer)
        FROM '{pq / "ppd" / "ppd.parquet"}') TO '{pq / "ppd" / "ppd2.parquet"}' (FORMAT parquet)"""
    )
    (pq / "ppd" / "ppd2.parquet").replace(pq / "ppd" / "ppd.parquet")
    _write(
        pq / "hmlr" / "pp-uprn-lookup-aug-2026.parquet",
        [[_tid(6), "6"], [_tid(7), "7"]],
        ["transaction_id", "value"],
    )
    return pq, staged.parent, out


def _run(env):
    pq, staged, out = env
    stats = ppd_lad(LAD, staged, out, pq)
    local = (
        duckdb.connect()
        .execute(f"SELECT * FROM '{out / f'lad={LAD}' / 'ppd_local.parquet'}' ORDER BY transaction_id")
        .fetchdf()
    )
    return stats, local, out / f"lad={LAD}"


def test_matching_rules(env):
    _, local, _ = _run(env)
    by = {r.transaction_id: r for r in local.itertuples()}
    assert (
        by[_tid(1)].uprn == 1 and by[_tid(1)].match_method == "address_match" and by[_tid(1)].match_confidence == "high"
    )
    assert by[_tid(2)].uprn == 1 and by[_tid(2)].match_confidence == "high"
    assert by[_tid(3)].uprn == 2  # flat 1 resolved
    assert _tid(4) not in by  # unit absent on PPD side must not match a flat
    assert _tid(5) not in by  # two UPRNs with the same address: not unique, so no match
    assert _tid(8) not in by  # record_status D dropped
    assert by[_tid(9)].uprn == 1 and by[_tid(9)].match_confidence == "low"  # street disagrees
    assert by[_tid(6)].match_method == "hmlr_lookup" and by[_tid(6)].uprn == 6
    # the lookup UPRN wins and the matcher's agreement is recorded as validation evidence
    assert by[_tid(7)].match_method == "hmlr_lookup" and bool(by[_tid(7)].am_agrees) is True


def test_lanes_do_not_leak(env):
    stats, local, lad_out = _run(env)
    con = duckdb.connect()
    op = con.execute(
        f"SELECT * FROM read_parquet('{lad_out / 'ppd_open.parquet'}', hive_partitioning=false) ORDER BY transaction_id"
    ).fetchdf()
    assert list(op.columns) == ["uprn", "transaction_id", "price", "date", "property_type", "old_new", "duration",
                                "ppd_category_type"]  # fmt: skip
    assert set(op.transaction_id) == {_tid(6), _tid(7)}  # only HMLR-lookup rows
    assert "address_match" in set(local.match_method)  # the local lane does carry derived matches
    # nothing address-like in any open file, per the publisher's own checks
    for f in ("ppd_open.parquet", "ppd_open_summary.parquet"):
        assert publish.validate_parquet(lad_out / f, require_geo=False, ogl=True) == []
    s_open = con.execute(f"SELECT * FROM '{lad_out / 'ppd_open_summary.parquet'}' ORDER BY uprn").fetchall()
    assert [r[0] for r in s_open] == [6, 7]  # summary only from lookup rows
    s_loc = {r[0]: r for r in con.execute(f"SELECT * FROM '{lad_out / 'ppd_local_summary.parquet'}'").fetchall()}
    assert s_loc[1][1] == 3  # sales_count (transactions 1, 2, 9)
    assert s_loc[1][2] == 100000 or s_loc[1][2] == 200000 or s_loc[1][2] == 180000
    assert s_loc[1][3] == dt.date(2010, 5, 5) and s_loc[1][4] == dt.date(2001, 1, 1)
    assert s_loc[1][2] == 200000  # last_price is the price on the last sale date
    assert (lad_out / "ppd_local.LICENCE.txt").exists() and "Contains HM Land Registry data" in (
        lad_out / "ppd_open.LICENCE.txt"
    ).read_text()
    assert stats["hmlr_lookup"] == 2


def test_ppd_all_resumable(env):
    pq, staged, out = env
    r1 = ppd_all(staged, out, pq)
    assert r1["done"] == [LAD] and not r1["failed"]
    r2 = ppd_all(staged, out, pq)
    assert r2["skipped"] == [LAD] and not r2["done"]
