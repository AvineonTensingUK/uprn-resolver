"""Tests for uprn_resolver.streets on synthetic data."""

import duckdb
import pytest

from uprn_resolver import streets

NAMES_HEADER = ",".join(streets.NAMES_COLS)


def _names_row(toid, name1, local_type="Named Road", lang1="", name2="", lang2="", place="", borough="", extra=""):
    r = {c: "" for c in streets.NAMES_COLS}
    r.update(ID=toid, NAME1=name1, NAME1_LANG=lang1, NAME2=name2, NAME2_LANG=lang2, TYPE="transportNetwork",
             LOCAL_TYPE=local_type, POPULATED_PLACE=place, DISTRICT_BOROUGH=borough)  # fmt: skip
    if extra:
        r["SAME_AS_DBPEDIA"] = extra  # unquoted comma -> 35 fields
    return ",".join(f'"{r[c]}"' if "," in r[c] and not extra else r[c] for c in streets.NAMES_COLS)


@pytest.fixture
def parquet_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(streets, "MIN_FREE_GB", 0)
    pdir = tmp_path / "parquet"
    src = tmp_path / "src"
    (src / "Data").mkdir(parents=True)
    (src / "Doc").mkdir()
    (src / "Doc" / "OS_Open_Names_Header.csv").write_text(NAMES_HEADER + "\n")
    rows = [
        _names_row("osgb4000000000000001", "High Street", place="Townsville"),
        _names_row("osgb4000000000000002", "High Street", place="Townsville"),  # 2nd TOID, same USRN
        _names_row("osgb4000000000000003", "Heol Fawr", lang1="cym", name2="High Road", lang2="eng", borough="Cardiff"),
        _names_row("osgb4000000000000004", "Lonely Lane", extra="http://dbpedia.org/Lonely,_Lane"),
        _names_row("osgb4000000000000005", "Not A Road", local_type="Railway Station"),
        _names_row("osgb4000000000000006", "Zed Road", place="B", borough="C"),  # USRN 30 (tie loser)
        _names_row("osgb4000000000000007", "Alpha Road", place="A"),  # USRN 30 (tie winner)
        _names_row("osgb4000000000000008", "Quoted, Name Road", place="Q"),
        _names_row("section1", "Section", local_type="Section Of Named Road"),
    ]
    (src / "Data" / "TQ28.csv").write_text("\n".join(rows) + "\n")
    (tmp_path / "road.csv").write_text(
        "CORRELATION_ID,IDENTIFIER_1,VERSION_NUMBER_1,VERSION_DATE_1,IDENTIFIER_2,VERSION_NUMBER_2,VERSION_DATE_2,CONFIDENCE\n"
        "a,osgb4000000000000001,,,10,,,ok\nb,osgb4000000000000002,,,10,,,ok\n"
        "c,osgb4000000000000003,,,20,,,ok\nd,osgb4000000000000004,,,40,,,ok\n"
        "e,osgb4000000000000006,,,30,,,ok\nf,osgb4000000000000007,,,30,,,ok\n"
        "g,osgb4000000000000008,,,50,,,ok\n"
    )
    (tmp_path / "bu.csv").write_text(
        "CORRELATION_ID,IDENTIFIER_1,VERSION_NUMBER_1,VERSION_DATE_1,IDENTIFIER_2,VERSION_NUMBER_2,VERSION_DATE_2,CONFIDENCE\n"
        "a,1001,,,10,,,ok\nb,1002,,,20,,,ok\nc,1003,,,999,,,ok\n"  # 999: unnamed USRN
        "d,1004,,,999,,,ok\ne,1004,,,30,,,ok\n"  # multi-USRN: prefer the named one
        "f,1005,,,30,,,ok\ng,1005,,,10,,,ok\n"  # multi, both named: smallest USRN (10)
        "h,1006,,,40,,,ok\ni,1007,,,50,,,ok\nj,1999,,,10,,,ok\n"  # 1999 not in the LAD
    )
    assert streets.convert_streets("open_names", src, pdir) == 7  # Named Road rows with TOID ids
    assert streets.convert_streets("road_usrn", tmp_path / "road.csv", pdir) == 7
    assert streets.convert_streets("uprn_usrn", tmp_path / "bu.csv", pdir) == 10
    return pdir


def test_convert_outputs(parquet_dir):
    q = lambda f, sql: duckdb.sql(sql.format(f=parquet_dir / "streets" / f)).fetchall()  # noqa: E731
    assert q("open_names.parquet", "select count(*) from '{f}' where toid not like 'osgb%'") == [(0,)]
    assert q("uprn_usrn.parquet", "select typeof(uprn), typeof(usrn) from '{f}' limit 1") == [("BIGINT", "BIGINT")]
    assert (parquet_dir / "manifest.json").exists()
    assert "streets/uprn_usrn.parquet" in (parquet_dir / "manifest.json").read_text()


def test_convert_unknown(tmp_path):
    with pytest.raises(ValueError):
        streets.convert_streets("nope", tmp_path, tmp_path)


def test_stage_streets(tmp_path, parquet_dir):
    staged = tmp_path / "data" / "staged"
    raw = tmp_path / "data" / "raw"
    (tmp_path / "data").mkdir(exist_ok=True)
    parquet_dir.rename(tmp_path / "data" / "parquet")
    (staged / "E1").mkdir(parents=True)
    duckdb.execute(
        f"COPY (SELECT * FROM (VALUES (1001::BIGINT),(1002),(1003),(1004),(1005),(1006),(1007),(1008)) t(uprn)) "
        f"TO '{staged / 'E1' / 'onsud.parquet'}' (FORMAT PARQUET)"
    )
    streets.stage_streets("E1", raw, staged)
    rows = {
        r[0]: r
        for r in duckdb.sql(
            f"select uprn, usrn, street_name, street_name_alt, locality, street_method, usrn_count "
            f"from '{staged / 'E1' / 'streets.parquet'}'"
        ).fetchall()
    }
    assert set(rows) == {1001, 1002, 1003, 1004, 1005, 1006, 1007}  # 1008 has no USRN link
    assert rows[1001] == (1001, 10, "High Street", None, "Townsville", "lids_usrn_open_names", 1)
    # Welsh name1 -> English name2 is street_name, Welsh is alt; locality falls back to district_borough
    assert rows[1002] == (1002, 20, "High Road", "Heol Fawr", "Cardiff", "lids_usrn_open_names", 1)
    assert rows[1003][2:] == (None, None, None, "lids_usrn", 1)
    assert rows[1004][:3] == (1004, 30, "Alpha Road") and rows[1004][6] == 2  # named beats unnamed
    assert rows[1005][1] == 10 and rows[1005][6] == 2  # smallest USRN among named
    assert rows[1006][2] == "Lonely Lane"  # row with an unquoted comma in SAME_AS_DBPEDIA survives
    assert rows[1007][2] == "Quoted, Name Road"


def test_usrn_name_tiebreak_is_lexicographic(tmp_path, parquet_dir):
    (tmp_path / "data").mkdir(exist_ok=True)
    parquet_dir.rename(tmp_path / "data" / "parquet")
    staged = tmp_path / "data" / "staged"
    (staged / "E1").mkdir(parents=True)
    duckdb.execute(f"COPY (SELECT 1004::BIGINT uprn) TO '{staged / 'E1' / 'onsud.parquet'}' (FORMAT PARQUET)")
    streets.stage_streets("E1", tmp_path / "data" / "raw", staged)
    out = duckdb.sql(f"select street_name, locality from '{staged / 'E1' / 'streets.parquet'}'").fetchall()
    assert out == [("Alpha Road", "A")]


def test_stage_noop_when_inputs_absent(tmp_path, capsys):
    (tmp_path / "staged" / "E1").mkdir(parents=True)
    streets.stage_streets("E1", tmp_path / "data" / "raw", tmp_path / "staged")
    assert not (tmp_path / "staged" / "E1" / "streets.parquet").exists()
    assert "skipping" in capsys.readouterr().out
