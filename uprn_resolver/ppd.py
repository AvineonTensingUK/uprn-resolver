"""HM Land Registry Price Paid Data (PPD): download, convert, match to UPRNs, per-LAD outputs.

Two lanes, kept strictly apart:

* OPEN lane (``ppd_open.parquet``, OGL v3): only transactions whose UPRN comes from HMLR's own
  transaction-to-UPRN lookup (``match_method='hmlr_lookup'``, Aug 2026 onwards). No address fields, no
  derived matches.
* LOCAL lane (``ppd_local.parquet``, never published): the same rows plus matches derived by comparing PPD
  address text with EPC/OSM address text (``match_method='address_match'``) and the PPD address fields.
  PPD address data is third-party (Royal Mail / OS) data: permitted for personal/non-commercial use only.

``pp-complete.csv`` is ~5.5 GB, headerless, fully quoted. It is streamed (never stored whole): the stream is cut
at line boundaries into chunks, each chunk is converted to a Parquet part and deleted, then the parts are merged
into ``data/parquet/ppd/ppd.parquet``. Free disk is checked before every chunk (abort below 6 GB).
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import time
from pathlib import Path

import duckdb

from .convert import _append_manifest, _free_gb

log = logging.getLogger(__name__)


def _q(p) -> str:
    """Escape a path for use inside single quotes in SQL (no surrounding quotes)."""
    return str(p).replace("'", "''")


URL = "https://price-paid-data.publicdata.landregistry.gov.uk/pp-complete.csv"
MIN_FREE_GB = 6.0
CHUNK_BYTES = 600 * 1024 * 1024
COLUMNS = [
    "transaction_id", "price", "date_of_transfer", "postcode", "property_type", "old_new", "duration",
    "paon", "saon", "street", "locality", "town_city", "district", "county", "ppd_category_type",
    "record_status",
]  # fmt: skip
ATTRIBUTION = (
    "Contains HM Land Registry data © Crown copyright and database right 2021. "
    "This data is licensed under the Open Government Licence v3.0."
)
HMLR_UPRN_NOTE = "UPRNs contain OS data © Crown copyright and database right (see publish/LICENCE.md section 6)."
LOCAL_LICENCE = """ppd_local.parquet - LOCAL USE ONLY, DO NOT PUBLISH OR REDISTRIBUTE

Contains HM Land Registry data © Crown copyright and database right 2021. This data is licensed under the
Open Government Licence v3.0 - EXCEPT the address fields (paon, saon, street, locality, town_city, postcode).
HMLR: "Price Paid Data contains address data processed against Ordnance Survey's AddressBase Premium product,
which incorporates Royal Mail's PAF(R) database (Address Data). Royal Mail and Ordnance Survey permit your use of
Address Data in the Price Paid Data: for personal and/or non-commercial use; to display for the purpose of
providing residential property price information services. If you want to use the Address Data in any other way,
you must contact Royal Mail."

Rows with match_method='address_match' were derived locally by comparing PPD address text with EPC address text
(EPC addresses are also restricted, OS/Royal Mail consent) and OpenStreetMap addr:* tags (ODbL). They are not
HMLR data and must never be published in an OGL file. Only match_method='hmlr_lookup' rows are in ppd_open.parquet.
"""
OPEN_LICENCE = f"""ppd_open.parquet - Open Government Licence v3.0

{ATTRIBUTION}

Only transactions with an HMLR-published transaction-to-UPRN lookup are included (match_method='hmlr_lookup',
transfers from 28 August 2026 onwards). No address fields. {HMLR_UPRN_NOTE}
"""

# ----------------------------------------------------------------------------- download / convert


def _check_disk(path: Path, min_free: float = MIN_FREE_GB) -> None:
    free = _free_gb(path)
    if free < min_free:
        raise RuntimeError(f"only {free:.1f} GB free (< {min_free} GB); aborting")


def _csv_chunk_to_parquet(csv: Path, out: Path) -> tuple[int, int]:
    """Convert one headerless CSV chunk to a Parquet part; returns (csv_lines, parquet_rows)."""
    n_lines = 0
    with open(csv, "rb") as f:
        for buf in iter(lambda: f.read(1 << 24), b""):
            n_lines += buf.count(b"\n")
    cols = ", ".join(f"'c{i}': 'VARCHAR'" for i in range(16))
    con = duckdb.connect()
    try:
        con.execute(
            f"""COPY (SELECT
                '{{' || upper(replace(replace(trim(c0), '{{', ''), '}}', '')) || '}}' AS transaction_id,
                TRY_CAST(c1 AS BIGINT) AS price,
                TRY_CAST(left(c2, 10) AS DATE) AS date_of_transfer,
                nullif(trim(c3), '') AS postcode, nullif(trim(c4), '') AS property_type,
                nullif(trim(c5), '') AS old_new, nullif(trim(c6), '') AS duration,
                nullif(trim(c7), '') AS paon, nullif(trim(c8), '') AS saon, nullif(trim(c9), '') AS street,
                nullif(trim(c10), '') AS locality, nullif(trim(c11), '') AS town_city,
                nullif(trim(c12), '') AS district, nullif(trim(c13), '') AS county,
                nullif(trim(c14), '') AS ppd_category_type, nullif(trim(c15), '') AS record_status
              FROM read_csv('{_q(csv)}', header=false, columns={{{cols}}}, quote='"', delim=',',
                            all_varchar=true, strict_mode=false))
              TO '{_q(out)}' (FORMAT PARQUET, COMPRESSION ZSTD)"""
        )
        n_pq = con.execute(f"SELECT count(*) FROM read_parquet('{_q(out)}')").fetchone()[0]
    finally:
        con.close()
    return n_lines, n_pq


def download_ppd(parquet_dir: Path, work_dir: Path | None = None, url: str = URL, keep_parts: bool = False) -> dict:
    """Stream pp-complete.csv -> data/parquet/ppd/ppd.parquet (all 16 columns, zstd). Resumable per chunk.

    Nothing larger than one chunk is ever on disk as CSV. Raises if a chunk's Parquet row count differs from its
    CSV line count. Manifest entry is written under the manifest file lock.
    """
    import requests

    parquet_dir = Path(parquet_dir)
    out_dir = parquet_dir / "ppd"
    parts = out_dir / ".parts"
    parts.mkdir(parents=True, exist_ok=True)
    work = Path(work_dir) if work_dir else out_dir / ".work"
    work.mkdir(parents=True, exist_ok=True)
    state_p = parts / "state.json"
    state = json.loads(state_p.read_text()) if state_p.exists() else {"offset": 0, "parts": [], "lines": 0}
    t0 = time.time()
    _check_disk(parquet_dir)
    with requests.get(
        url, stream=True, timeout=120, headers={"Range": f"bytes={state['offset']}-"} if state["offset"] else {}
    ) as r:
        if state["offset"] and r.status_code != 206:
            raise RuntimeError(f"server ignored Range (status {r.status_code}); delete {parts} and restart")
        r.raise_for_status()
        total = int(r.headers.get("Content-Length", 0)) + state["offset"]
        etag = r.headers.get("ETag", "")
        if state.get("etag") and etag and state["etag"] != etag:
            raise RuntimeError("remote file changed during resumed download; delete the parts dir and restart")
        state["etag"] = etag
        last_mod = r.headers.get("Last-Modified", "")
        buf = bytearray()
        committed = state["offset"]

        def flush(data: bytes) -> None:
            nonlocal committed
            _check_disk(parquet_dir)
            idx = len(state["parts"])
            csv = work / f"chunk-{idx:04d}.csv"
            csv.write_bytes(data)
            pq = parts / f"part-{idx:04d}.parquet"
            tmp = pq.with_suffix(".tmp")
            n_csv, n_pq = _csv_chunk_to_parquet(csv, tmp)
            if n_csv != n_pq:
                tmp.unlink(missing_ok=True)
                raise RuntimeError(f"chunk {idx}: {n_pq} parquet rows != {n_csv} csv lines")
            tmp.replace(pq)
            csv.unlink()
            committed += len(data)
            state["parts"].append(pq.name)
            state["offset"] = committed
            state["lines"] += n_csv
            state_p.write_text(json.dumps(state))
            log.info("ppd: chunk %d done (%d rows, %.0f%% of %.2f GB)", idx, n_pq, 100 * committed / total, total / 1e9)

        for piece in r.iter_content(1 << 20):
            buf += piece
            if len(buf) >= CHUNK_BYTES:
                cut = buf.rfind(b"\n") + 1
                if cut:
                    flush(bytes(buf[:cut]))
                    del buf[:cut]
        if buf:
            if not buf.endswith(b"\n"):
                buf += b"\n"
            flush(bytes(buf))
    if committed != total:
        raise RuntimeError(f"downloaded {committed} bytes, expected {total}; rerun to resume")
    final = out_dir / "ppd.parquet"
    tmp = out_dir / "ppd.parquet.tmp"
    con = duckdb.connect()
    try:
        con.execute(f"SET temp_directory='{_q(parquet_dir.parent / 'tmp')}'")
        con.execute("SET memory_limit='4GB'")
        con.execute("SET preserve_insertion_order=false")
        src = f"read_parquet('{_q(parts)}/part-*.parquet')"
        con.execute(
            f"COPY (SELECT * FROM {src} ORDER BY postcode, transaction_id) TO '{_q(tmp)}' "
            "(FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 500000)"
        )
        n_final = con.execute(f"SELECT count(*) FROM read_parquet('{_q(tmp)}')").fetchone()[0]
    finally:
        con.close()
    if n_final != state["lines"]:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"merged {n_final} rows != {state['lines']} csv lines")
    tmp.replace(final)
    entry = {
        "dataset": "ppd",
        "source": "pp-complete.csv",
        "source_url": url,
        "source_size": total,
        "source_last_modified": last_mod,
        "rows": n_final,
        "parquet_size": final.stat().st_size,
        "columns": COLUMNS,
        "converted_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "licence": "OGL v3.0 for the data; ADDRESS FIELDS (postcode, paon, saon, street, locality, town_city, "
        "district, county) are Royal Mail/OS third-party data: personal/non-commercial use, or display for "
        "residential property price information services. LOCAL ONLY, never published.",
        "attribution": ATTRIBUTION,
    }
    _append_manifest(parquet_dir, "ppd/ppd.parquet", entry)
    if not keep_parts:
        shutil.rmtree(parts, ignore_errors=True)
        shutil.rmtree(work, ignore_errors=True) if not work_dir else None
    log.info("ppd: %d rows in %.0fs", n_final, time.time() - t0)
    return entry


# ----------------------------------------------------------------------------- normalisation
# One SQL definition (macros), used by the matcher; python helpers call the same macros (used by the tests).
#
# An address (PPD saon+paon+street, or EPC address1..3) is normalised to upper-case alphanumerics; number ranges
# ('12-14', '12 to 14', '12/14') become '12~14'. Components extracted from the text:
#   flat   : identifier after FLAT/APARTMENT/APT/UNIT/ROOM/SUITE/STUDIO/MAISONETTE/PLOT ('FLAT 3' -> '3',
#            'FLAT A' -> 'A'); else a bare leading unit before a house number ('3 12 FOO ROAD' -> flat 3, house 12);
#            else the floor descriptor ('FIRST FLOOR FLAT' -> 'FIRST').
#   house  : first number token ('12', '12A', '12~14') of what is left.
#   name   : a building name (lines with no digits) -- used only when the PPD address has no house number.
# Street agreement: the PPD street (abbreviations expanded) occurs inside the candidate's address text.

_FLAT_KW = "FLAT|APARTMENT|APT|UNIT|ROOM|SUITE|STUDIO|MAISONETTE|PLOT"
_FLAT_RE = rf"(?:{_FLAT_KW}) ([0-9]+[A-Z]?|[A-Z])( |$)"
_FLOORS = "GROUND|FIRST|SECOND|THIRD|FOURTH|FIFTH|TOP|BASEMENT|LOWER|UPPER|GARDEN|ATTIC|MIDDLE"
_FLOOR_RE = rf"({_FLOORS}) (?:FLOOR|FLAT|MAISONETTE)"
_FLOOR_FULL = rf"(?:{_FLOORS}) (?:FLOOR|FLAT|MAISONETTE)(?: FLAT| FLOOR| MAISONETTE)?"
_BARE_RE = r"^([0-9]+[A-Z]?|[A-Z]) ([0-9]+[A-Z]?)( |$)"
_NUM_RE = r"(?:^| )([0-9]+[A-Z]?(?:~[0-9]+[A-Z]?)?)(?: |$)"

_ABBREV = {"ST": "STREET", "RD": "ROAD", "AVE": "AVENUE", "LN": "LANE", "DR": "DRIVE", "CL": "CLOSE",
           "CT": "COURT", "GDNS": "GARDENS"}  # fmt: skip


def _n_street_sql() -> str:
    expr = "' ' || coalesce(n_addr(x), '') || ' '"
    for ab, full in _ABBREV.items():
        expr = f"regexp_replace({expr}, ' {ab} ', ' {full} ', 'g')"
    return f"CREATE OR REPLACE MACRO n_street(x) AS nullif(trim({expr}), '');"


_N_STREET = _n_street_sql()
_MACROS = f"""
CREATE OR REPLACE MACRO n_pc(x) AS nullif(upper(regexp_replace(coalesce(x, ''), '[^A-Za-z0-9]', '', 'g')), '');
CREATE OR REPLACE MACRO n_addr(x) AS nullif(trim(regexp_replace(regexp_replace(regexp_replace(
    upper(coalesce(x, '')), '([0-9]+[A-Z]?) *(?:-|/|TO) *([0-9]+[A-Z]?)', '\\1~\\2', 'g'),
    '[^A-Z0-9~ ]', ' ', 'g'), ' +', ' ', 'g')), '');
{_N_STREET}
CREATE OR REPLACE MACRO a_nofloor(t) AS regexp_replace(' ' || coalesce(t, '') || ' ', ' {_FLOOR_FULL}', ' ', 'g');
CREATE OR REPLACE MACRO a_flat(t) AS coalesce(
    nullif(regexp_extract(a_nofloor(t), '{_FLAT_RE}', 1), ''),
    nullif(regexp_extract(trim(regexp_replace(a_nofloor(t), '{_FLAT_RE}', ' ')), '{_BARE_RE}', 1), ''),
    nullif(regexp_extract(t, '{_FLOOR_RE}', 1), ''));
-- text with the flat phrase / bare unit / floor descriptor removed
CREATE OR REPLACE MACRO a_rest(t) AS nullif(trim(regexp_replace(regexp_replace(
    regexp_replace(a_nofloor(t), ' {_FLAT_RE}', ' ', 'g'),
    '^ *([0-9]+[A-Z]?|[A-Z]) ([0-9]+[A-Z]?( |$))', '\\2'), ' +', ' ', 'g')), '');
CREATE OR REPLACE MACRO a_house(t) AS nullif(regexp_extract(a_rest(t), '{_NUM_RE}', 1), '');
"""


def connect_macros(con) -> None:
    for stmt in _MACROS.split(";\n"):
        s = "\n".join(ln for ln in stmt.splitlines() if not ln.strip().startswith("--")).strip().rstrip(";")
        if s:
            con.execute(s)


def _eval(expr: str, *args):
    con = duckdb.connect()
    connect_macros(con)
    try:
        return con.execute(f"SELECT {expr}", list(args)).fetchone()[0]
    finally:
        con.close()


def norm_postcode(x):
    return _eval("n_pc(?)", x)


def norm_address(x):
    return _eval("n_addr(?)", x)


def norm_street(x):
    return _eval("n_street(?)", x)


def address_parts(text: str) -> dict:
    """(flat, house) extracted from free address text (already joined, any case). For tests/diagnostics."""
    t = norm_address(text)
    return {"flat": _eval("a_flat(?)", t), "house": _eval("a_house(?)", t)}


# ----------------------------------------------------------------------------- matcher


def build_keys(con, ppd_tbl: str, epc_tbl: str, osm_tbl: str | None) -> None:
    """Create ppd_k (one row per transaction) and cand_k (one row per candidate UPRN address key).

    Input tables: ppd_tbl(transaction_id, postcode, paon, saon, street); epc_tbl(uprn, address1..3, postcode, rk)
    (rk = 1 for the UPRN's latest certificate; ALL certificates are used); osm_tbl(uprn, osm_housenumber,
    osm_housename, osm_unit, osm_street, osm_postcode, building_level) or None.
    cand_k columns: src, uprn, pc, flat, house, name, full_st (text for street containment), rk
    """
    con.execute(
        f"""CREATE OR REPLACE TABLE ppd_k AS
        WITH b AS (
            SELECT transaction_id, n_pc(postcode) AS pc,
                   n_addr(concat_ws(' ', saon, paon, street)) AS full_t, n_addr(concat_ws(' ', saon, paon)) AS t,
                   n_street(street) AS st, n_addr(paon) AS tp
            FROM {ppd_tbl}
        )
        SELECT transaction_id, pc, st,
               a_flat(t) AS flat, a_house(t) AS house,
               -- name-only addresses: PAON without digits (building name), flat words removed
               CASE WHEN a_house(t) IS NULL THEN nullif(a_rest(tp), '') END AS name
        FROM b WHERE pc IS NOT NULL AND (tp IS NOT NULL OR t IS NOT NULL)"""
    )
    # EPC: parts from the joined lines; names from the individual lines that carry no digits
    con.execute(
        f"""CREATE OR REPLACE TABLE epc_t AS
        SELECT uprn, rk, n_pc(postcode) AS pc, n_addr(concat_ws(' ', address1, address2, address3)) AS t,
               n_addr(address1) AS l1, n_addr(address2) AS l2, n_addr(address3) AS l3
        FROM {epc_tbl} WHERE uprn IS NOT NULL"""
    )
    con.execute(
        """CREATE OR REPLACE TABLE epc_k AS
        SELECT DISTINCT 'epc' AS src, uprn, pc, a_flat(t) AS flat, a_house(t) AS house, NULL::VARCHAR AS name,
               ' ' || n_street(t) || ' ' AS full_st, rk
        FROM epc_t WHERE pc IS NOT NULL AND t IS NOT NULL
        UNION
        SELECT DISTINCT 'epc', uprn, pc, a_flat(t), NULL, nm, ' ' || n_street(t) || ' ', rk
        FROM (SELECT uprn, pc, t, rk, unnest([a_rest(l1), a_rest(l2), a_rest(l3)]) AS nm FROM epc_t) x
        WHERE pc IS NOT NULL AND nm IS NOT NULL AND NOT regexp_matches(nm, '[0-9]')"""
    )
    if osm_tbl:
        con.execute(
            f"""CREATE OR REPLACE TABLE osm_k AS
            SELECT DISTINCT 'osm' AS src, uprn, n_pc(osm_postcode) AS pc, nullif(n_addr(osm_unit), '') AS flat,
                   CASE WHEN osm_housenumber !~ '[;,]' THEN a_house(n_addr(osm_housenumber)) END AS house,
                   CASE WHEN osm_housenumber IS NULL THEN a_rest(n_addr(osm_housename)) END AS name,
                   ' ' || coalesce(n_street(osm_street), '') || ' ' AS full_st, 1 AS rk
            FROM {osm_tbl} WHERE NOT coalesce(building_level, false)
              AND (osm_housenumber IS NOT NULL OR osm_housename IS NOT NULL)"""
        )
    else:
        con.execute(
            "CREATE OR REPLACE TABLE osm_k (src VARCHAR, uprn BIGINT, pc VARCHAR, flat VARCHAR, house VARCHAR, "
            "name VARCHAR, full_st VARCHAR, rk INT)"
        )
    con.execute("CREATE OR REPLACE TABLE cand_k AS SELECT * FROM epc_k UNION ALL SELECT * FROM osm_k")


def run_match(con) -> None:
    """Build `ppd_match(transaction_id, uprn, confidence, evidence, has_epc, has_osm, by_number)` from ppd_k, cand_k.

    Rules: exact normalised postcode; house number token equal (or, when the PPD address has no house number, the
    building name equal); flat/unit key equal, where absent on one side it must be absent on the other (a bare
    '12 FOO ROAD' never matches 'FLAT 1, 12 FOO ROAD'); then ONE distinct candidate UPRN or no match. If several
    UPRNs qualify, street agreement is a tie-break: exactly one street-agreeing UPRN is accepted ('medium' at best).
    Confidence: high = unique, house-number match, street agrees; medium = unique with street unknown, a name match
    with street agreeing, or a tie-break; low = street disagrees, or a name match with the street unknown.
    """
    con.execute(
        """CREATE OR REPLACE TABLE cand AS
        SELECT p.transaction_id, c.uprn, c.src, c.rk, (p.house IS NOT NULL) AS by_number,
            CASE WHEN p.st IS NULL OR c.full_st = '  ' THEN 'unknown'
                 WHEN contains(c.full_st, ' ' || p.st || ' ') THEN 'agree' ELSE 'disagree' END AS sf
        FROM ppd_k p JOIN cand_k c ON c.pc = p.pc
         AND ((p.house IS NOT NULL AND c.house = p.house)
              OR (p.house IS NULL AND p.name IS NOT NULL AND c.name = p.name))
         AND coalesce(c.flat, '') = coalesce(p.flat, '')"""
    )
    con.execute(
        """CREATE OR REPLACE TABLE cand_u AS
        SELECT transaction_id, uprn, bool_or(by_number) AS by_number,
               -- a candidate agrees when any of its keys agrees on street; disagrees only if all do
               bool_or(sf = 'agree') AS s_agree, bool_and(sf = 'disagree') AS s_dis,
               bool_or(src = 'epc') AS has_epc, bool_or(src = 'osm') AS has_osm
        FROM cand GROUP BY 1, 2"""
    )
    con.execute(
        """CREATE OR REPLACE TABLE ppd_match AS
        WITH n AS (
            SELECT *, count(*) OVER (PARTITION BY transaction_id) AS n_all,
                   count(*) FILTER (s_agree) OVER (PARTITION BY transaction_id) AS n_agree
            FROM cand_u
        )
        SELECT transaction_id, uprn,
            CASE WHEN n_all > 1 THEN 'medium'
                 WHEN by_number AND s_agree THEN 'high'
                 WHEN by_number AND s_dis THEN 'low'
                 WHEN by_number THEN 'medium'
                 WHEN s_agree THEN 'medium'
                 ELSE 'low' END AS confidence,
            CASE WHEN n_all = 1 THEN 'unique' ELSE 'street_tiebreak' END AS evidence,
            has_epc, has_osm, by_number
        FROM n WHERE n_all = 1 OR (n_agree = 1 AND s_agree)"""
    )


# ----------------------------------------------------------------------------- work tables / per-LAD build


def _pq_list(files) -> str:
    return "[" + ", ".join(f"'{_q(f)}'" for f in files) + "]"


def _epc_files(parquet_dir: Path) -> list[Path]:
    out: list[Path] = []
    for sub in ("domestic", "non_domestic"):
        d = Path(parquet_dir) / "epc_ew" / sub
        if d.exists():
            out += sorted(d.glob("certificates-*.parquet"))
    return out


def _lookup_files(parquet_dir: Path) -> list[Path]:
    return sorted((Path(parquet_dir) / "hmlr").glob("pp-uprn-lookup-*.parquet"))


def work_dir(parquet_dir: Path) -> Path:
    return Path(parquet_dir).parent / "tmp" / "ppd_work"


def prepare_work_tables(parquet_dir: Path, rebuild: bool = False) -> dict:
    """Build the LOCAL scratch tables once (data/tmp/ppd_work/, never published): EPC addresses (all certificates,
    distinct per UPRN+address text, sorted by uprn, rk = recency rank) and PPD rows (D rows dropped, postcode key)."""
    wd = work_dir(parquet_dir)
    wd.mkdir(parents=True, exist_ok=True)
    epc_out, ppd_out = wd / "epc_addr.parquet", wd / "ppd_work.parquet"
    ppd_pq = Path(parquet_dir) / "ppd" / "ppd.parquet"
    res = {}
    con = duckdb.connect()
    try:
        con.execute(f"SET temp_directory='{_q(wd)}'")
        con.execute("SET memory_limit='6GB'")
        connect_macros(con)
        if rebuild or not epc_out.exists():
            _check_disk(wd)
            files = _epc_files(parquet_dir)
            if not files:
                raise FileNotFoundError("no E&W EPC parquet in data/parquet/epc_ew")
            tmp = epc_out.with_suffix(".tmp")
            con.execute(
                f"""COPY (
                  SELECT uprn, address1, address2, address3, postcode,
                         row_number() OVER (PARTITION BY uprn ORDER BY d DESC NULLS LAST) AS rk
                  FROM (SELECT TRY_CAST(uprn AS BIGINT) AS uprn, address1, address2, address3, postcode,
                               max(TRY_CAST(left(lodgement_date, 10) AS DATE)) AS d
                        FROM read_parquet({_pq_list(files)}, union_by_name=true)
                        WHERE TRY_CAST(uprn AS BIGINT) IS NOT NULL
                        GROUP BY 1, 2, 3, 4, 5)
                  ORDER BY uprn) TO '{_q(tmp)}' (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 200000)"""
            )
            tmp.replace(epc_out)
        if rebuild or not ppd_out.exists():
            _check_disk(wd)
            tmp = ppd_out.with_suffix(".tmp")
            con.execute(
                f"""COPY (SELECT transaction_id, price, date_of_transfer AS date, postcode, n_pc(postcode) AS pc,
                    property_type, old_new, duration, paon, saon, street, locality, town_city, ppd_category_type
                    FROM read_parquet('{_q(ppd_pq)}') WHERE coalesce(record_status, 'A') <> 'D'
                    ORDER BY pc) TO '{_q(tmp)}' (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 200000)"""
            )
            tmp.replace(ppd_out)
        for k, p in (("epc_addr", epc_out), ("ppd_work", ppd_out)):
            res[k] = con.execute(f"SELECT count(*) FROM read_parquet('{_q(p)}')").fetchone()[0]
    finally:
        con.close()
    return res


def _summary_sql(src: str) -> str:
    return f"""SELECT uprn, count(*) AS sales_count,
        arg_max(price, (date, transaction_id)) AS last_price, max(date) AS last_sale_date, min(date) AS first_sale_date
        FROM {src} GROUP BY uprn ORDER BY uprn"""


def ppd_lad(
    lad: str,
    staged_dir: Path,
    out_dir: Path,
    parquet_dir: Path,
    *,
    include_address_match: bool = True,
    memory_limit: str = "6GB",
) -> dict:
    """Build ppd_local.parquet, ppd_open.parquet and the per-UPRN summaries for one LAD.

    The LAD's UPRNs are the staged ONSUD ones. PPD rows are those whose postcode is a postcode of the LAD's UPRNs,
    plus any transaction whose HMLR lookup UPRN is in the LAD. The address matcher also runs on rows that have an
    HMLR lookup UPRN (never used for the output UPRN; recorded in am_confidence / am_agrees as validation evidence).
    A postcode straddling two LADs is matched against each LAD's UPRNs separately (uniqueness is per LAD).
    """
    sdir = Path(staged_dir) / lad
    onsud = sdir / "onsud.parquet"
    if not onsud.exists():
        raise FileNotFoundError(f"{onsud} missing; stage ONSUD first")
    wd = work_dir(parquet_dir)
    ppd_w, epc_a = wd / "ppd_work.parquet", wd / "epc_addr.parquet"
    if not ppd_w.exists() or not epc_a.exists():
        prepare_work_tables(parquet_dir)
    lad_out = Path(out_dir) / f"lad={lad}"
    lad_out.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    try:
        tmpd = wd
        con.execute(f"SET temp_directory='{_q(tmpd)}'")
        con.execute(f"SET memory_limit='{memory_limit}'")
        connect_macros(con)
        con.execute(
            f"CREATE TEMP TABLE lad_uprn AS SELECT uprn, any_value(postcode) AS postcode "
            f"FROM read_parquet('{_q(onsud)}') WHERE uprn IS NOT NULL GROUP BY uprn"
        )
        con.execute(
            "CREATE TEMP TABLE lad_pc AS SELECT DISTINCT n_pc(postcode) AS pc FROM lad_uprn WHERE postcode IS NOT NULL"
        )
        lk = _lookup_files(parquet_dir)
        if lk:
            con.execute(
                f"""CREATE TEMP TABLE lk AS
                SELECT transaction_id, TRY_CAST(value AS BIGINT) AS uprn FROM read_parquet({_pq_list(lk)})
                QUALIFY row_number() OVER (PARTITION BY transaction_id ORDER BY value) = 1"""
            )
            con.execute("DELETE FROM lk WHERE uprn IS NULL OR uprn NOT IN (SELECT uprn FROM lad_uprn)")
        else:
            con.execute("CREATE TEMP TABLE lk (transaction_id VARCHAR, uprn BIGINT)")
        con.execute(
            f"""CREATE TEMP TABLE ppd AS SELECT * FROM read_parquet('{_q(ppd_w)}')
            WHERE pc IN (SELECT pc FROM lad_pc) OR transaction_id IN (SELECT transaction_id FROM lk)"""
        )
        n_ppd = con.execute("SELECT count(*) FROM ppd").fetchone()[0]
        ran = include_address_match and n_ppd > 0
        if ran:
            con.execute(
                f"CREATE TEMP TABLE epc_raw AS SELECT * FROM read_parquet('{_q(epc_a)}') "
                "WHERE uprn IN (SELECT uprn FROM lad_uprn)"
            )
            osm_pq = Path(out_dir) / f"lad={lad}" / "streets_odbl.parquet"
            osm_tbl = None
            if osm_pq.exists():
                con.execute(
                    f"""CREATE TEMP TABLE osm_raw AS SELECT s.uprn, s.osm_housenumber, s.osm_housename, s.osm_unit,
                    s.osm_addr_street AS osm_street, coalesce(s.osm_addr_postcode, l.postcode) AS osm_postcode,
                    s.building_level_address AS building_level
                    FROM read_parquet('{_q(osm_pq)}') s JOIN lad_uprn l USING (uprn)
                    WHERE s.osm_housenumber IS NOT NULL OR s.osm_housename IS NOT NULL"""
                )
                osm_tbl = "osm_raw"
            build_keys(con, "ppd", "epc_raw", osm_tbl)
            run_match(con)
        else:
            con.execute(
                "CREATE TABLE ppd_match (transaction_id VARCHAR, uprn BIGINT, confidence VARCHAR, evidence VARCHAR, "
                "has_epc BOOLEAN, has_osm BOOLEAN, by_number BOOLEAN)"
            )
        con.execute(
            """CREATE TEMP TABLE m AS
            SELECT p.*, coalesce(l.uprn, a.uprn) AS uprn,
                CASE WHEN l.uprn IS NOT NULL THEN 'hmlr_lookup' WHEN a.uprn IS NOT NULL THEN 'address_match' END
                    AS match_method,
                CASE WHEN l.uprn IS NOT NULL THEN 'authoritative' ELSE a.confidence END AS match_confidence,
                a.confidence AS am_confidence,
                CASE WHEN l.uprn IS NOT NULL AND a.uprn IS NOT NULL THEN l.uprn = a.uprn END AS am_agrees,
                coalesce(a.has_epc, false) AS am_has_epc
            FROM ppd p LEFT JOIN lk l USING (transaction_id) LEFT JOIN ppd_match a USING (transaction_id)"""
        )
        local_cols = """uprn, transaction_id, price, date, property_type, old_new, duration, match_method,
            match_confidence, am_confidence, am_agrees, ppd_category_type, paon, saon, street, locality, town_city,
            postcode"""
        for out, sql in (
            (lad_out / "ppd_local.parquet",
             f"SELECT {local_cols} FROM m WHERE uprn IS NOT NULL ORDER BY uprn, date, transaction_id"),
            (lad_out / "ppd_open.parquet",
             """SELECT uprn, transaction_id, price, date, property_type, old_new, duration, ppd_category_type
                FROM m WHERE match_method = 'hmlr_lookup' ORDER BY uprn, date, transaction_id"""),
            (lad_out / "ppd_local_summary.parquet", _summary_sql("(SELECT * FROM m WHERE uprn IS NOT NULL)")),
            (lad_out / "ppd_open_summary.parquet",
             _summary_sql("(SELECT * FROM m WHERE match_method = 'hmlr_lookup')")),
        ):  # fmt: skip
            tmp = out.with_name(out.name + ".tmp")
            con.execute(f"COPY ({sql}) TO '{_q(tmp)}' (FORMAT PARQUET, COMPRESSION ZSTD)")
            tmp.replace(out)
        (lad_out / "ppd_local.LICENCE.txt").write_text(LOCAL_LICENCE)
        (lad_out / "ppd_open.LICENCE.txt").write_text(OPEN_LICENCE)
        stats = {
            k: int(v)
            for k, v in con.execute(
                """SELECT count(*) AS ppd_rows,
                    count(*) FILTER (match_method = 'hmlr_lookup') AS hmlr_lookup,
                    count(*) FILTER (match_method = 'address_match' AND match_confidence = 'high') AS am_high,
                    count(*) FILTER (match_method = 'address_match' AND match_confidence = 'medium') AS am_medium,
                    count(*) FILTER (match_method = 'address_match' AND match_confidence = 'low') AS am_low,
                    count(*) FILTER (match_method IS NULL) AS unmatched,
                    count(DISTINCT uprn) AS uprns,
                    count(*) FILTER (match_method = 'address_match' AND am_has_epc) AS am_with_epc
                FROM m"""
            )
            .fetchdf()
            .iloc[0]
            .items()
        }
        for conf in ("high", "medium", "low"):
            stats[f"val_{conf}_n"], stats[f"val_{conf}_correct"] = (
                int(x) for x in con.execute(
                    "SELECT count(*), count(*) FILTER (am_agrees) FROM m WHERE match_method = 'hmlr_lookup' "
                    "AND am_confidence = ?", [conf]
                ).fetchone()
            )  # fmt: skip
        stats["lad"] = lad
        (lad_out / "ppd.stats.json").write_text(json.dumps(stats))
        return stats
    finally:
        con.close()


def ppd_all(
    staged_dir: Path,
    out_dir: Path,
    parquet_dir: Path,
    lads: list[str] | None = None,
    skip_existing: bool = True,
    **kw,
) -> dict:
    """Run ppd_lad for every England & Wales LAD with staged ONSUD (E/W codes). Resumable: skips LADs that
    already have ppd.stats.json. Returns {done, skipped, failed}."""
    staged_dir = Path(staged_dir)
    cands = lads or sorted(p.name for p in staged_dir.iterdir() if p.is_dir() and re.match(r"[EW]\d", p.name))
    res: dict = {"done": [], "skipped": [], "failed": []}
    todo = [
        lad
        for lad in cands
        if (staged_dir / lad / "onsud.parquet").exists()
        and not (skip_existing and (Path(out_dir) / f"lad={lad}" / "ppd.stats.json").exists())
    ]
    res["skipped"] = [lad for lad in cands if lad not in todo]
    if todo:
        prepare_work_tables(parquet_dir)
    for lad in todo:
        try:
            Path(out_dir).mkdir(parents=True, exist_ok=True)
            _check_disk(Path(out_dir), 2.0)
            ppd_lad(lad, staged_dir, out_dir, parquet_dir, **kw)
            res["done"].append(lad)
        except Exception as e:  # noqa: BLE001
            log.error("ppd %s failed: %s", lad, e)
            res["failed"].append(lad)
    return res
