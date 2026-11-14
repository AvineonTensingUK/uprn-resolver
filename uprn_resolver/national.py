"""National roll-up over per-LAD outputs (``out/lad=*/``): merged buildings, cross-LAD
building UPRN counts, aggregated stats, per-LAD summary and a static HTML report.

Works on whatever LADs exist at run time. Per-LAD files are never rewritten; results go
to ``out/national/``. No address text is read or emitted.
"""

from __future__ import annotations

import html
import json
import time
from pathlib import Path

import duckdb
import pyarrow.parquet as pq

from .build import _geo_meta

OUTLIER_N = 10
METHOD_COLOURS = {"toid_point_in_polygon": "#1b9e77", "uprn_point_in_polygon": "#d95f02", "none": "#7570b3"}


def _con(out_dir: Path):
    con = duckdb.connect()
    tmp = Path(out_dir).resolve() / "national" / ".tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    con.execute(f"SET temp_directory='{tmp}'")
    con.execute("SET preserve_insertion_order=false")
    return con


def _lad_dirs(out_dir: Path) -> list[Path]:
    return sorted(p.parent for p in Path(out_dir).glob("lad=*/stats.json"))


def _geo_summary(files: list[Path]):
    """Union of geometry types and bbox from each file's GeoParquet metadata."""
    types: set[str] = set()
    bbox = None
    for f in files:
        md = (pq.ParquetFile(f).schema_arrow.metadata or {}).get(b"geo")
        if not md:
            continue
        col = json.loads(md)["columns"]["geometry"]
        types.update(col.get("geometry_types", []))
        b = col.get("bbox")
        if b:
            bbox = (
                list(b)
                if bbox is None
                else [min(bbox[0], b[0]), min(bbox[1], b[1]), max(bbox[2], b[2]), max(bbox[3], b[3])]
            )
    return sorted(types) or ["Polygon"], bbox


def _merge(stats_list: list[dict]) -> dict:
    out: dict = {}
    for s in stats_list:
        for k, v in s.items():
            if k in ("lad", "built_at") or isinstance(v, (str, list)):
                continue
            if isinstance(v, bool):
                out[k] = out.get(k, 0) + int(v)  # *_present: number of LADs
            elif isinstance(v, (int, float)):
                out[k] = out.get(k, 0) + v
            elif isinstance(v, dict):
                d = out.setdefault(k, {})
                for kk, vv in v.items():
                    if isinstance(vv, dict):
                        e = d.setdefault(kk, {"count": 0})
                        e["count"] += vv.get("count", 0)
                        if "of_both_matched" in vv:
                            e["of_both_matched"] = e.get("of_both_matched", 0) + vv["of_both_matched"]
                    elif isinstance(vv, (int, float)) and not isinstance(vv, bool):
                        d[kk] = d.get(kk, 0) + vv
    return out


def _pct(n, d):
    return round(100.0 * n / d, 4) if d else 0.0


def _aggregate(stats_list: list[dict]) -> dict:
    agg = _merge(stats_list)
    total = agg.get("total_uprns", 0)
    for k, v in agg.items():
        if not isinstance(v, dict):
            continue
        if k == "methods_disagree":
            v["pct"] = _pct(v["count"], v.get("of_both_matched", 0))
        elif all(isinstance(x, dict) and "count" in x for x in v.values()) and v:
            for x in v.values():
                x["pct"] = _pct(x["count"], total)
        elif "count" in v:
            v["pct"] = _pct(v["count"], total)
    agg["lads"] = len(stats_list)
    agg["lad_codes"] = sorted(s["lad"] for s in stats_list)
    agg.pop("buildings_written", None)  # double counts straddlers; see buildings_national
    return agg


def _summary_rows(stats_list: list[dict]) -> list[dict]:
    rows = []
    for s in stats_list:
        t = s.get("total_uprns", 0)
        r = {"lad": s["lad"], "total_uprns": t}
        for m, v in (s.get("match_method") or {}).items():
            r[f"pct_{m}"] = _pct(v["count"], t)
        r["pct_street_name"] = _pct((s.get("with_street_name") or {}).get("count", 0), t)
        r["pct_parcel"] = _pct((s.get("with_parcel") or {}).get("count", 0), t)
        for g in ("single", "parcel_finer", "shared", "none"):
            r[f"outline_{g}"] = (s.get("outline_granularity") or {}).get(g, 0)
            r[f"pct_outline_{g}"] = _pct(r[f"outline_{g}"], t)
        r["pct_epc"] = _pct(s["with_epc"]["count"], t) if s.get("epc_present") and s.get("with_epc") else None
        r["pct_outline"] = round(100.0 - r.get("pct_none", 0.0), 4)
        rows.append(r)
    return rows


def national(out_dir, report_path) -> dict:
    t0 = time.time()
    out_dir, report_path = Path(out_dir), Path(report_path)
    lads = _lad_dirs(out_dir)
    if not lads:
        raise FileNotFoundError(f"no out/lad=*/stats.json under {out_dir}")
    nat = out_dir / "national"
    nat.mkdir(parents=True, exist_ok=True)
    stats_list = [json.loads((d / "stats.json").read_text()) for d in lads]
    codes = {s["lad"] for s in stats_list}
    ufiles = [d / "uprn.parquet" for d in lads if (d / "uprn.parquet").exists()]
    bfiles = [d / "buildings.parquet" for d in lads if (d / "buildings.parquet").exists()]
    con = _con(out_dir)
    ulist = "[" + ",".join(f"'{f}'" for f in ufiles) + "]"
    blist = "[" + ",".join(f"'{f}'" for f in bfiles) + "]"

    # 1. merged buildings (dedupe by building_id)
    gtypes, bbox = _geo_summary(bfiles)
    geo = _geo_meta(gtypes, bbox).replace("'", "''")
    bpath = nat / "buildings.parquet"
    con.execute(f"""COPY (SELECT building_id, any_value(geometry) AS geometry
        FROM read_parquet({blist}) GROUP BY building_id ORDER BY building_id)
        TO '{bpath}' (FORMAT PARQUET, COMPRESSION ZSTD, KV_METADATA {{geo: '{geo}'}})""")
    n_b_rows = con.execute(f"SELECT count(*) FROM read_parquet({blist})").fetchone()[0]
    n_b = con.execute(f"SELECT count(*) FROM read_parquet('{bpath}')").fetchone()[0]

    # 2. cross-LAD building counts
    con.execute(f"""CREATE TEMP TABLE up AS SELECT uprn, building_id, uprns_in_building, lad
        FROM read_parquet({ulist}, hive_partitioning=true) WHERE building_id IS NOT NULL""")
    cpath = nat / "building_uprn_counts.parquet"
    con.execute(f"""COPY (SELECT building_id, count(DISTINCT uprn)::INTEGER AS uprns_in_building_national,
        count(DISTINCT lad)::INTEGER AS lads_spanned FROM up GROUP BY building_id
        ORDER BY building_id) TO '{cpath}' (FORMAT PARQUET, COMPRESSION ZSTD)""")
    n_span, n_span_uprns = con.execute(f"""
        SELECT count(*), coalesce(sum(uprns_in_building_national), 0)
        FROM read_parquet('{cpath}') WHERE lads_spanned > 1""").fetchone()
    n_changed = con.execute(f"""SELECT count(*) FROM up JOIN read_parquet('{cpath}') c USING (building_id)
        WHERE up.uprns_in_building IS DISTINCT FROM c.uprns_in_building_national""").fetchone()[0]
    n_uprn_b = con.execute("SELECT count(*) FROM up").fetchone()[0]
    cross = {
        "buildings_rows_per_lad": int(n_b_rows),
        "buildings_national": int(n_b),
        "duplicate_building_rows": int(n_b_rows - n_b),
        "buildings_spanning_multiple_lads": int(n_span),
        "uprns_in_spanning_buildings": int(n_span_uprns),
        "uprns_with_changed_count": int(n_changed),
        "uprns_with_building": int(n_uprn_b),
    }

    # 3. aggregate stats + per-LAD summary
    agg = _aggregate(stats_list)
    agg["buildings_national"] = cross["buildings_national"]
    agg["cross_lad"] = cross
    agg["built_at"] = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    (nat / "stats.json").write_text(json.dumps(agg, indent=2))
    rows = _summary_rows(stats_list)
    cols = sorted({k for r in rows for k in r}, key=lambda k: (k != "lad", k != "total_uprns", k))
    tbl = {c: [r.get(c) for r in rows] for c in cols}
    import pyarrow as pa

    pq.write_table(pa.table(tbl), nat / "lad_summary.parquet", compression="zstd")

    # 4. report
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(_html(agg, rows, cross))
    con.close()
    agg["runtime_s"] = round(time.time() - t0, 2)
    assert codes == set(agg["lad_codes"])
    return agg


# ---------------------------------------------------------------- HTML


def _e(x) -> str:
    return html.escape(str(x))


def _bars(title: str, items: list[tuple[str, int, float]], colours=None) -> str:
    mx = max([p for _, _, p in items] + [0.0001])
    rows = "".join(
        f"<tr><td>{_e(k)}</td><td class=n>{n:,}</td><td class=n>{p:.2f}%</td>"
        f"<td class=b><i style='width:{100 * p / mx:.1f}%;background:"
        f"{(colours or {}).get(k, '#4c72b0')}'></i></td></tr>"
        for k, n, p in items
    )
    return f"<div class=card><h3>{_e(title)}</h3><table>{rows}</table></div>"


def _dist(agg: dict, key: str, order=None):
    d = agg.get(key) or {}
    tot = sum(v["count"] if isinstance(v, dict) else v for v in d.values()) or 1
    keys = order or sorted(d, key=lambda k: -(d[k]["count"] if isinstance(d[k], dict) else d[k]))
    out = []
    for k in keys:
        if k in d:
            n = d[k]["count"] if isinstance(d[k], dict) else d[k]
            out.append((k, n, 100.0 * n / tot))
    return out


def _outliers(rows, key, low=True):
    vals = sorted((r for r in rows if r.get(key) is not None), key=lambda r: r[key], reverse=not low)
    return {r["lad"] for r in vals[:OUTLIER_N]} if len(rows) > OUTLIER_N else set()


def _html(agg: dict, rows: list[dict], cross: dict) -> str:
    total = agg.get("total_uprns", 0)
    tiles = [
        ("LADs", agg["lads"]),
        ("UPRNs", total),
        ("Buildings (deduped)", cross["buildings_national"]),
        ("With outline %", 100 - agg["match_method"].get("none", {}).get("pct", 0)),
        ("With street name %", (agg.get("with_street_name") or {}).get("pct", 0)),
        ("With parcel %", (agg.get("with_parcel") or {}).get("pct", 0)),
    ]
    tiles_h = "".join(
        f"<div class=tile><b>{v:,.2f}</b><span>{_e(k)}</span></div>"
        if isinstance(v, float)
        else f"<div class=tile><b>{v:,}</b><span>{_e(k)}</span></div>"
        for k, v in tiles
    )
    cards = [
        _bars("match_method", _dist(agg, "match_method"), METHOD_COLOURS),
        _bars("outline_granularity", _dist(agg, "outline_granularity", ["single", "parcel_finer", "shared", "none"])),
        _bars("uprns_in_building", _dist(agg, "uprns_in_building", ["1", "2-5", "6-20", ">20"])),
        _bars("uprns_in_parcel", _dist(agg, "uprns_in_parcel", ["1", "2-5", "6-20", ">20"])),
    ]
    if agg.get("epc_built_form"):
        cards.append(_bars("EPC built form", _dist(agg, "epc_built_form")))
    cross_h = (
        "<div class=card><h3>Cross-LAD buildings</h3><table>"
        + "".join(f"<tr><td>{_e(k.replace('_', ' '))}</td><td class=n>{v:,}</td></tr>" for k, v in cross.items())
        + "</table></div>"
    )

    o_out = _outliers(rows, "pct_outline")
    o_str = _outliers(rows, "pct_street_name")
    o_sh = _outliers(rows, "pct_outline_shared", low=False)
    methods = sorted({k[4:] for r in rows for k in r if k.startswith("pct_") and k[4:] in agg["match_method"]})
    head = (
        ["lad", "total_uprns", "pct_outline"]
        + [f"pct_{m}" for m in methods]
        + ["pct_street_name", "pct_parcel", "pct_epc"]
        + [f"pct_outline_{g}" for g in ("single", "parcel_finer", "shared", "none")]
    )
    th = "".join(f"<th data-k={i}>{_e(h)}</th>" for i, h in enumerate(head))
    flag = {"pct_outline": o_out, "pct_street_name": o_str, "pct_outline_shared": o_sh}
    body = []
    for r in rows:
        tds = []
        for h in head:
            v = r.get(h)
            cls = " class=out" if r["lad"] in flag.get(h, ()) else ""
            if v is None:
                tds.append("<td data-v=-1>-</td>")
            elif h == "lad":
                tds.append(f"<td data-v='{_e(v)}'>{_e(v)}</td>")
            elif isinstance(v, float):
                tds.append(f"<td{cls} data-v={v}>{v:.2f}</td>")
            else:
                tds.append(f"<td{cls} data-v={v}>{v:,}</td>")
        body.append("<tr>" + "".join(tds) + "</tr>")
    return f"""<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>National UPRN report</title>
<style>body{{font:14px system-ui,sans-serif;margin:16px;color:#222;background:#fff}}
.tiles{{display:flex;flex-wrap:wrap;gap:10px}}.tile{{border:1px solid #ccc;border-radius:6px;padding:8px 14px}}
.tile b{{display:block;font-size:20px}}.tile span{{color:#666;font-size:12px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:12px;margin:14px 0}}
.card{{border:1px solid #ccc;border-radius:6px;padding:8px 12px}}h3{{margin:.2em 0}}
table{{border-collapse:collapse;width:100%}}td,th{{padding:2px 6px;border-bottom:1px solid #eee;text-align:left}}
td.n,#t td{{text-align:right}}td.b{{width:35%}}td.b i{{display:block;height:10px;border-radius:2px}}
th{{cursor:pointer;background:#f4f4f4;position:sticky;top:0}}td.out{{background:#ffd9d9;font-weight:600}}
.wrap{{max-height:70vh;overflow:auto}}
@media(prefers-color-scheme:dark){{body{{background:#161616;color:#ddd}}.card,.tile{{border-color:#444}}
th{{background:#2a2a2a}}td,th{{border-color:#333}}td.out{{background:#5a2323}}}}</style></head><body>
<h1>National UPRN report</h1><p>{agg["lads"]} LADs. Generated {_e(agg["built_at"])}.</p>
<div class=tiles>{tiles_h}</div><div class=grid>{"".join(cards)}{cross_h}</div>
<h2>Per-LAD summary</h2><p>Click a header to sort. Highlighted: {OUTLIER_N} lowest outline %,
lowest street name %, highest shared %.</p>
<div class=wrap><table id=t><thead><tr>{th}</tr></thead><tbody>{"".join(body)}</tbody></table></div>
<script>const t=document.getElementById('t'),b=t.tBodies[0];let dir=1;
t.tHead.onclick=e=>{{const i=e.target.dataset.k;if(i===undefined)return;dir=-dir;
const rs=[...b.rows];rs.sort((x,y)=>{{const a=x.cells[i].dataset.v,c=y.cells[i].dataset.v,
n=parseFloat(a),m=parseFloat(c);return dir*(isNaN(n)||isNaN(m)?a.localeCompare(c):n-m)}});
rs.forEach(r=>b.appendChild(r))}};</script></body></html>"""
