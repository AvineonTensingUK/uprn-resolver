"""LOCAL-ONLY full-address table per LAD (never published).

Address text comes from E&W EPC certificates, whose address fields are NOT OGL (published with OS / Royal
Mail consent, restricted use), then (fallback ``ppd``) from HM Land Registry Price Paid Data address fields
(``ppd_local.parquet``, also restricted by Royal Mail / OS) for UPRNs with no EPC address. On rows where both
exist, the PPD house number + street agree with the EPC text ~98% (postcode ~100%) versus ~90-92% for OSM
addr:* matches, so in ``resolve`` PPD ranks after EPC and before OSM.
Output is ``out_dir/lad=<lad>/address_local.parquet`` plus a LICENCE sidecar.
``publish.validate_parquet`` rejects it (address/postcode/town column names), and it must stay out of release
assembly.
"""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

import duckdb

log = logging.getLogger(__name__)

MIN_YEAR = 2007
# (address_source, directory under parquet_dir, glob)
SOURCES = (
    ("epc_domestic", "epc_ew/domestic", "certificates-*.parquet"),
    ("epc_non_domestic", "epc_ew/non_domestic", "certificates-*.parquet"),
    ("epc_dec", "epc_ew/display", "certificates-*.parquet"),
    ("epc_scot_domestic", "epc_scot/domestic", "*.parquet"),
    ("epc_scot_non_domestic", "epc_scot/non_domestic", "*.parquet"),
)
# output column -> accepted source column names (lower-case; first present wins)
ALIASES = {
    "uprn": ("uprn", "osg_reference_number"),
    "address1": ("address1", "address_1", "address_line_1"),
    "address2": ("address2", "address_2", "address_line_2"),
    "address3": ("address3", "address_3", "address_line_3"),
    "address": ("address",),
    "posttown": ("posttown", "post_town"),
    "postcode": ("postcode", "post_code"),
    "lodgement_date": ("lodgement_date", "date_of_assessment", "lodgement_datetime"),
    "certificate_number": ("certificate_number", "lmk_key", "report_reference_number"),
}
LICENCE_TEXT = """address_local.parquet - LOCAL USE ONLY, DO NOT PUBLISH OR REDISTRIBUTE

The EPC-derived address fields (address_line1..3, post_town, postcode, single_line_address) come from the
Energy Performance of Buildings Register for England and Wales. Those address fields are NOT published under
the Open Government Licence: they are published with Ordnance Survey / Royal Mail consent for restricted
use only. Rows with licence = 'EPC-restricted' contain such text.

Only street_name_open / locality_open (OS Open Names, OGL) and postcode_onsud (ONSUD, OGL) are open data.
Rows with licence = 'open-partial' contain only open fields.

Rows with address_source = 'ppd' (licence = 'PPD-restricted') take their address fields from HM Land Registry
Price Paid Data (latest sale with match_method 'hmlr_lookup' or match_confidence 'high'; paon, saon, street,
locality, town_city, postcode as lodged, upper case).
Contains HM Land Registry data (c) Crown copyright and database right 2021. This data is licensed under the Open
Government Licence v3.0. The address data in PPD is restricted by Royal Mail / Ordnance Survey: personal and
non-commercial use only. It is NOT covered by the OGL grant.

Scottish EPC data had its address columns dropped at conversion, so Scottish UPRNs have no EPC address.
This file is rejected by uprn_resolver.publish validation by design; never add it to a release.
"""


def _q(p) -> str:
    return "'" + str(p).replace("'", "''") + "'"


def _norm_pc(expr: str) -> str:
    return f"nullif(upper(regexp_replace({expr}, '[^A-Za-z0-9]', '', 'g')), '')"


def address_local(lad: str, staged_dir: Path, out_dir: Path, parquet_dir: Path) -> dict:
    sdir = Path(staged_dir) / lad
    onsud = sdir / "onsud.parquet"
    if not onsud.exists():
        raise FileNotFoundError(f"{onsud} missing; stage ONSUD first")
    streets = sdir / "streets.parquet"
    lad_out = Path(out_dir) / f"lad={lad}"
    lad_out.mkdir(parents=True, exist_ok=True)
    today = dt.date.today().isoformat()

    con = duckdb.connect()
    try:
        tmpd = Path(parquet_dir).parent / "tmp"
        if tmpd.exists():
            con.execute(f"SET temp_directory={_q(tmpd)}")
        con.execute("SET memory_limit='4GB'")
        con.execute(
            f"CREATE TEMP TABLE lad_uprn AS SELECT uprn, any_value(postcode) AS postcode "
            f"FROM read_parquet({_q(onsud)}) WHERE uprn IS NOT NULL GROUP BY uprn"
        )
        con.execute("CREATE TEMP TABLE lad_uprn_s AS SELECT CAST(uprn AS VARCHAR) AS uprn FROM lad_uprn")
        parts: list[str] = []
        for source, sub, pat in SOURCES:
            files = sorted((Path(parquet_dir) / sub).glob(pat)) if (Path(parquet_dir) / sub).exists() else []
            if not files:
                continue
            src = f"read_parquet([{', '.join(_q(f) for f in files)}], union_by_name=true)"
            have = {r[0].lower(): r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {src}").fetchall()}
            sel = {}
            for out_c, cands in ALIASES.items():
                hit = next((have[c] for c in cands if c in have), None)
                sel[out_c] = f'CAST("{hit}" AS VARCHAR)' if hit else "NULL::VARCHAR"
            if sel["uprn"] == "NULL::VARCHAR":
                continue
            cols = ", ".join(f"{v} AS {k}" for k, v in sel.items())
            tbl = f"raw_{len(parts)}"
            con.execute(
                f"CREATE TEMP TABLE {tbl} AS SELECT * FROM (SELECT {cols} FROM {src}) "
                f"WHERE uprn IN (SELECT uprn FROM lad_uprn_s)"
            )
            parts.append(f"SELECT *, '{source}' AS src, {len(parts)} AS pref FROM {tbl}")
        has_epc = bool(parts)
        if has_epc:
            union = " UNION ALL ".join(f"({p})" for p in parts)
            con.execute(
                f"""CREATE TEMP TABLE epc_best AS
                SELECT CAST(uprn AS BIGINT) AS uprn, src, address1, address2, address3, posttown, postcode, address,
                       d AS cert_date
                FROM (
                    SELECT *, TRY_CAST(left(lodgement_date, 10) AS DATE) AS d0,
                        CASE WHEN TRY_CAST(left(lodgement_date, 10) AS DATE)
                                  BETWEEN DATE '{MIN_YEAR}-01-01' AND DATE '{today}'
                             THEN TRY_CAST(left(lodgement_date, 10) AS DATE) END AS d
                    FROM ({union})
                    QUALIFY row_number() OVER (PARTITION BY uprn ORDER BY d DESC NULLS LAST, pref ASC,
                                               certificate_number DESC NULLS LAST) = 1
                )"""
            )
        else:
            con.execute(
                "CREATE TEMP TABLE epc_best (uprn BIGINT, src VARCHAR, address1 VARCHAR, address2 VARCHAR, "
                "address3 VARCHAR, posttown VARCHAR, postcode VARCHAR, address VARCHAR, cert_date DATE)"
            )
        ppd_p = lad_out / "ppd_local.parquet"
        if ppd_p.exists():
            con.execute(
                f"""CREATE TEMP TABLE ppd_best AS
                SELECT uprn, nullif(trim(saon), '') AS saon, nullif(trim(paon), '') AS paon,
                       nullif(trim(street), '') AS street, nullif(trim(locality), '') AS locality,
                       nullif(trim(town_city), '') AS town_city, nullif(trim(postcode), '') AS postcode,
                       date AS ppd_date
                FROM read_parquet({_q(ppd_p)})
                WHERE uprn IS NOT NULL AND (match_method = 'hmlr_lookup' OR match_confidence = 'high')
                QUALIFY row_number() OVER (PARTITION BY uprn ORDER BY date DESC NULLS LAST,
                                           transaction_id DESC NULLS LAST) = 1"""
            )
        else:
            con.execute(
                "CREATE TEMP TABLE ppd_best (uprn BIGINT, saon VARCHAR, paon VARCHAR, street VARCHAR, "
                "locality VARCHAR, town_city VARCHAR, postcode VARCHAR, ppd_date DATE)"
            )
        if streets.exists():
            con.execute(
                f"CREATE TEMP TABLE st AS SELECT uprn, street_name, locality FROM read_parquet({_q(streets)}) "
                "QUALIFY row_number() OVER (PARTITION BY uprn ORDER BY usrn NULLS LAST) = 1"
            )
        else:
            con.execute("CREATE TEMP TABLE st (uprn BIGINT, street_name VARCHAR, locality VARCHAR)")
        n = "nullif(trim({}), '')"
        sql = f"""
            WITH j AS (
                SELECT l.uprn, l.postcode AS postcode_onsud, s.street_name AS street_name_open,
                       s.locality AS locality_open, e.src,
                       {n.format("e.address1")} AS a1, {n.format("e.address2")} AS a2,
                       {n.format("e.address3")} AS a3, {n.format("e.posttown")} AS pt,
                       {n.format("e.postcode")} AS pc, {n.format("e.address")} AS full_addr, e.cert_date,
                       p.saon AS p_saon, p.paon AS p_paon, p.street AS p_street, p.locality AS p_loc,
                       p.town_city AS p_town, p.postcode AS p_pc, p.ppd_date
                FROM lad_uprn l LEFT JOIN epc_best e USING (uprn) LEFT JOIN st s USING (uprn)
                     LEFT JOIN ppd_best p USING (uprn)
            ),
            k0 AS (
                SELECT *, (a1 IS NOT NULL OR a2 IS NOT NULL OR a3 IS NOT NULL OR pt IS NOT NULL OR pc IS NOT NULL
                           OR full_addr IS NOT NULL) AS used_epc
                FROM j
            ),
            k AS (
                SELECT *, (NOT used_epc AND (p_paon IS NOT NULL OR p_street IS NOT NULL OR p_saon IS NOT NULL))
                          AS used_ppd,
                       -- numbered paon reads as "12 HIGH STREET"; a house name stays a separate element
                       CASE WHEN p_paon ~ '^[0-9]' THEN nullif(concat_ws(' ', p_paon, p_street), '')
                            ELSE nullif(concat_ws(', ', p_paon, p_street), '') END AS p_house
                FROM k0
            )
            SELECT uprn,
                   CASE WHEN used_epc THEN coalesce(a1, full_addr)
                        WHEN used_ppd THEN nullif(concat_ws(', ', p_saon, p_house), '') END AS address_line1,
                   CASE WHEN used_epc THEN a2 WHEN used_ppd THEN p_loc END AS address_line2,
                   CASE WHEN used_epc THEN a3 END AS address_line3,
                   CASE WHEN used_epc THEN pt WHEN used_ppd THEN p_town END AS post_town,
                   CASE WHEN used_epc THEN pc WHEN used_ppd THEN p_pc END AS postcode,
                   CASE WHEN used_epc THEN nullif(concat_ws(', ', coalesce(a1, full_addr), a2, a3, pt, pc), '')
                        WHEN used_ppd THEN nullif(concat_ws(', ', p_saon, p_house, p_loc, p_town, p_pc), '')
                        ELSE nullif(concat_ws(', ', street_name_open, locality_open, postcode_onsud), '') END
                        AS single_line_address,
                   street_name_open, locality_open, postcode_onsud,
                   CASE WHEN used_epc THEN src
                        WHEN used_ppd THEN 'ppd'
                        WHEN street_name_open IS NOT NULL OR locality_open IS NOT NULL THEN 'open_partial'
                        ELSE 'none' END AS address_source,
                   CASE WHEN used_epc THEN cert_date WHEN used_ppd THEN ppd_date END AS address_cert_date,
                   CASE WHEN used_epc THEN 'EPC-restricted' WHEN used_ppd THEN 'PPD-restricted'
                        ELSE 'open-partial' END AS licence,
                   CASE WHEN (CASE WHEN used_epc THEN pc WHEN used_ppd THEN p_pc END) IS NOT NULL
                             AND postcode_onsud IS NOT NULL
                        THEN {_norm_pc("(CASE WHEN used_epc THEN pc ELSE p_pc END)")}
                             <> {_norm_pc("postcode_onsud")} END AS postcode_mismatch
            FROM k ORDER BY uprn"""
        out = lad_out / "address_local.parquet"
        tmp = out.with_name(out.name + ".tmp")
        con.execute(f"COPY ({sql}) TO {_q(tmp)} (FORMAT parquet, COMPRESSION zstd)")
        tmp.replace(out)
        total, by_src, mism, comp = (
            con.execute(f"SELECT count(*) FROM read_parquet({_q(out)})").fetchone()[0],
            dict(con.execute(
                f"SELECT address_source, count(*) FROM read_parquet({_q(out)}) GROUP BY 1").fetchall()),
            con.execute(f"SELECT count(*) FILTER (postcode_mismatch) FROM read_parquet({_q(out)})").fetchone()[0],
            con.execute(
                f"SELECT count(*) FILTER (postcode_mismatch IS NOT NULL) FROM read_parquet({_q(out)})"
            ).fetchone()[0],
        )  # fmt: skip
    finally:
        con.close()
    (lad_out / "address_local.LICENCE.txt").write_text(LICENCE_TEXT)
    return {"lad": lad, "uprns": total, "by_source": by_src, "postcode_mismatch": mism,
            "postcode_compared": comp, "path": str(out)}  # fmt: skip


def address_local_all(
    staged_dir: Path, out_dir: Path, parquet_dir: Path, lads: list[str] | None = None, skip_existing: bool = True
) -> dict:
    """Run address_local for every LAD with staged onsud + streets. Resumable via existing outputs."""
    staged_dir = Path(staged_dir)
    cands = lads or sorted(p.name for p in staged_dir.iterdir() if p.is_dir())
    res: dict = {"built": [], "skipped": [], "failed": []}
    for lad in cands:
        s = staged_dir / lad
        if not (s / "onsud.parquet").exists() or not (s / "streets.parquet").exists():
            res["skipped"].append(lad)
            continue
        if skip_existing and (Path(out_dir) / f"lad={lad}" / "address_local.parquet").exists():
            res["skipped"].append(lad)
            continue
        try:
            address_local(lad, staged_dir, out_dir, parquet_dir)
            res["built"].append(lad)
        except Exception as e:  # noqa: BLE001
            log.error("address_local %s failed: %s", lad, e)
            res["failed"].append(lad)
    return res
