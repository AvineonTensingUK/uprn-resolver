"""QA report for a built LAD: a single static HTML file with tables and a sampled map.

No address text is read or emitted. Reprojection BNG -> WGS84 uses the OSTN15 inverse transform from
``bng`` (DuckDB spatial ``ST_Transform`` with the OSTN15 grid), not a Helmert approximation.
"""

from __future__ import annotations

import html
import json
from pathlib import Path

import duckdb

from uprn_resolver import bng
from uprn_resolver.convert import _lad, _q

MAX_UPRNS = 5000
MAX_BUILDINGS = 2000
MAX_LINES = 500
MAX_PARCELS = 1000
N_SUSPICIOUS = 25

LEAFLET_CSS = "https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
LEAFLET_JS = "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"

METHOD_COLOURS = {
    "toid_point_in_polygon": "#1b9e77",
    "uprn_point_in_polygon": "#d95f02",
    "none": "#7570b3",
}


def _ll(x: str, y: str) -> str:
    return bng.duckdb_st_transform_inverse_sql(f"ST_Point({x}, {y})")


def _pts(con, sql: str) -> list[dict]:
    cur = con.execute(sql)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


def _round(v, n=1):
    return None if v is None else round(float(v), n)


def _collect(con, stats: dict, has_parcels: bool = False) -> dict:
    # Sample of buildings (deterministic by hash), UPRNs in them, plus the
    # interesting extras: disagreements and UPRNs with a point but no building.
    con.execute(f"""
        CREATE TEMP TABLE sb AS
        SELECT building_id, geometry FROM bld
        ORDER BY hash(building_id) LIMIT {MAX_BUILDINGS}""")
    con.execute(f"""
        CREATE TEMP TABLE su AS
        (SELECT uprn FROM u WHERE building_id IN (SELECT building_id FROM sb)
           AND NOT coalesce(methods_disagree, false)
         ORDER BY hash(uprn) LIMIT {MAX_UPRNS})
        UNION
        (SELECT uprn FROM u WHERE methods_disagree ORDER BY dist DESC NULLS LAST, uprn
         LIMIT {MAX_LINES})
        UNION
        (SELECT uprn FROM u WHERE x IS NOT NULL AND building_id IS NULL
         ORDER BY hash(uprn) LIMIT 500)""")
    # Also keep buildings that the disagreeing UPRNs fall in, so lines have context.
    con.execute("""
        INSERT INTO sb SELECT building_id, geometry FROM bld
        WHERE building_id IN (SELECT building_id FROM u WHERE uprn IN
              (SELECT uprn FROM su) AND building_id IS NOT NULL)
          AND building_id NOT IN (SELECT building_id FROM sb)""")

    feats = []
    for r in con.execute(f"""
        SELECT building_id, ST_AsGeoJSON({bng.duckdb_st_transform_inverse_sql("geometry")}) FROM sb""").fetchall():
        feats.append(
            {"type": "Feature", "properties": {"kind": "building", "id": r[0][:8]}, "geometry": json.loads(r[1])}
        )
    buildings = {"type": "FeatureCollection", "features": feats}

    def gj(geom_json, props):
        return {"type": "Feature", "properties": props, "geometry": json.loads(geom_json)}

    upts, tpts, lines = [], [], []
    rows = con.execute(f"""
        SELECT uprn, toid, match_method, uprns_in_building, methods_disagree, dist,
               street_name, outline_granularity, uprns_in_parcel,
               ST_AsGeoJSON({_ll("x", "y")}),
               CASE WHEN toid_x IS NOT NULL THEN ST_AsGeoJSON({_ll("toid_x", "toid_y")}) END
        FROM u WHERE uprn IN (SELECT uprn FROM su) AND x IS NOT NULL""").fetchall()
    for uprn, toid, mm, n, dis, dist, street, gran, n_par, g, tg in rows:
        upts.append(
            gj(
                g,
                {
                    "kind": "uprn",
                    "uprn": uprn,
                    "toid": toid,
                    "method": mm,
                    "n_in_building": n,
                    "disagree": bool(dis),
                    "street": street,
                    "granularity": gran,
                    "n_in_parcel": n_par,
                },
            )
        )
        if tg and (dis or len(tpts) < MAX_UPRNS):
            tpts.append(gj(tg, {"kind": "toid", "toid": toid, "uprn": uprn}))
        if dis and tg and len(lines) < MAX_LINES:
            a, b = json.loads(g)["coordinates"], json.loads(tg)["coordinates"]
            lines.append(
                {
                    "type": "Feature",
                    "properties": {"kind": "disagree", "uprn": uprn, "dist_m": _round(dist)},
                    "geometry": {"type": "LineString", "coordinates": [a, b]},
                }
            )
    fc = lambda f: {"type": "FeatureCollection", "features": f}  # noqa: E731
    parcels = []
    if has_parcels:  # QA only: parcels of the sampled UPRNs; never published
        for pid, g in con.execute(f"""
            SELECT p.parcel_inspire_id,
                   ST_AsGeoJSON({bng.duckdb_st_transform_inverse_sql("ST_GeomFromWKB(p.geom_wkb)")})
            FROM parcels p WHERE p.parcel_inspire_id IN
              (SELECT parcel_inspire_id FROM u WHERE uprn IN (SELECT uprn FROM su))
            ORDER BY hash(p.parcel_inspire_id) LIMIT {MAX_PARCELS}""").fetchall():
            parcels.append(gj(g, {"kind": "parcel", "id": pid}))
    return {"buildings": buildings, "uprns": fc(upts), "toids": fc(tpts), "lines": fc(lines), "parcels": fc(parcels)}


def _suspicious(con) -> dict:
    n = N_SUSPICIOUS
    return {
        "largest_uprns_in_building": _pts(
            con,
            f"""
            SELECT building_id, max(uprns_in_building) AS uprns_in_building,
                   round(avg(x), 0) AS x, round(avg(y), 0) AS y
            FROM u WHERE building_id IS NOT NULL GROUP BY building_id
            ORDER BY uprns_in_building DESC, building_id LIMIT {n}""",
        ),
        "largest_disagreements": _pts(
            con,
            f"""
            SELECT uprn, toid, building_id, round(dist, 1) AS dist_m, x, y, toid_x, toid_y
            FROM u WHERE methods_disagree ORDER BY dist DESC NULLS LAST, uprn LIMIT {n}""",
        ),
        "point_without_building": _pts(
            con,
            f"""
            SELECT uprn, toid, x, y, toid_count FROM u
            WHERE x IS NOT NULL AND building_id IS NULL ORDER BY uprn LIMIT {n}""",
        ),
    }


def _table(headers, rows) -> str:
    h = "".join(f"<th>{html.escape(str(c))}</th>" for c in headers)
    b = "".join("<tr>" + "".join(f"<td>{html.escape(str(c))}</td>" for c in r) + "</tr>" for r in rows)
    return f"<table><thead><tr>{h}</tr></thead><tbody>{b}</tbody></table>"


def _render(stats, extra, susp, geo) -> str:
    total = stats["total_uprns"]
    pc = lambda n: f"{100.0 * n / total:.2f}%" if total else "0%"  # noqa: E731
    head = [
        ("LAD", stats["lad"]),
        ("Built at", stats.get("built_at", "")),
        ("Total UPRNs", f"{total:,}"),
        ("With point", f"{stats['with_point']:,}"),
        ("With TOID", f"{stats['with_toid']:,}"),
        ("With TOID point", f"{stats['with_toid_point']:,}"),
        ("With multiple TOIDs", f"{stats['with_multiple_toids']:,}"),
        ("Buildings written", f"{stats['buildings_written']:,}"),
    ]
    mm = _table(
        ["match_method", "count", "pct"],
        [(k, f"{v['count']:,}", f"{v['pct']}%") for k, v in stats["match_method"].items()],
    )
    d = stats["methods_disagree"]
    dis = _table(
        ["disagree", "of both matched", "pct"], [(f"{d['count']:,}", f"{d['of_both_matched']:,}", f"{d['pct']}%")]
    )
    hist = _table(
        ["uprns_in_building", "UPRNs", "share"], [(k, f"{v:,}", pc(v)) for k, v in stats["uprns_in_building"].items()]
    )
    toid = _table(["TOID multiplicity", "UPRNs", "share"], [(r[0], f"{r[1]:,}", pc(r[1])) for r in extra["toid_mult"]])
    hm = _table(
        ["HMLR table", "present", "UPRNs covered", "pct"],
        [
            ("PPD", stats["hmlr_ppd_present"], f"{stats['with_ppd']['count']:,}", f"{stats['with_ppd']['pct']}%"),
            (
                "INSPIRE",
                stats["hmlr_inspire_present"],
                f"{stats['with_inspire']['count']:,}",
                f"{stats['with_inspire']['pct']}%",
            ),
        ],
    )
    cov = (
        _table(
            ["coverage", "UPRNs", "pct"],
            [
                ("street_name", f"{stats['with_street_name']['count']:,}", f"{stats['with_street_name']['pct']}%"),
                ("INSPIRE parcel", f"{stats['with_parcel']['count']:,}", f"{stats['with_parcel']['pct']}%"),
                ("EPC", f"{stats['with_epc']['count']:,}", f"{stats['with_epc']['pct']}%"),
            ],
        )
        if "with_street_name" in stats
        else "<p>(not available: rebuild)</p>"
    )
    parc = _table(
        ["uprns_in_parcel", "UPRNs", "share"],
        [(k, f"{v:,}", pc(v)) for k, v in stats.get("uprns_in_parcel", {}).items()],
    )
    gran = _table(
        ["outline_granularity", "UPRNs", "share"],
        [(k, f"{v:,}", pc(v)) for k, v in stats.get("outline_granularity", {}).items()],
    )
    epcf = _table(
        ["epc_built_form", "UPRNs", "share"], [(k, f"{v:,}", pc(v)) for k, v in stats.get("epc_built_form", {}).items()]
    )
    cards = "".join(f"<div class=card><b>{html.escape(str(v))}</b><span>{html.escape(k)}</span></div>" for k, v in head)
    s1 = _table(
        ["building_id", "uprns_in_building", "x", "y"],
        [(r["building_id"][:12], r["uprns_in_building"], r["x"], r["y"]) for r in susp["largest_uprns_in_building"]],
    )
    s2 = _table(
        ["uprn", "toid", "dist_m", "x", "y", "toid_x", "toid_y"],
        [
            (r["uprn"], r["toid"], r["dist_m"], r["x"], r["y"], r["toid_x"], r["toid_y"])
            for r in susp["largest_disagreements"]
        ],
    )
    s3 = _table(
        ["uprn", "toid", "toid_count", "x", "y"],
        [(r["uprn"], r["toid"], r["toid_count"], r["x"], r["y"]) for r in susp["point_without_building"]],
    )
    data = json.dumps(geo, separators=(",", ":")).replace("</", "<\\/")
    colours = json.dumps(METHOD_COLOURS)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>UPRN QA {html.escape(stats["lad"])}</title>
<link rel="stylesheet" href="{LEAFLET_CSS}">
<style>
body{{font:14px system-ui,sans-serif;margin:0 auto;max-width:1100px;padding:0 16px 40px;color:#222}}
.cards{{display:flex;flex-wrap:wrap;gap:8px}}
.card{{border:1px solid #ccc;border-radius:6px;padding:8px 12px;min-width:130px}}
.card b{{display:block;font-size:18px}} .card span{{color:#666;font-size:12px}}
table{{border-collapse:collapse;margin:6px 0 16px}} td,th{{border:1px solid #ddd;padding:3px 8px;text-align:left}}
#map{{height:560px;border:1px solid #ccc}} .legend span{{margin-right:12px}}
i.sw{{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:4px}}
</style></head><body>
<h1>UPRN resolver QA: {html.escape(stats["lad"])}</h1>
<div class=cards>{cards}</div>
<h2>Match method</h2>{mm}
<h2>Methods disagree</h2>{dis}
<h2>UPRNs per building</h2>{hist}
<h2>Outline granularity</h2>{gran}
<h2>UPRNs per INSPIRE parcel</h2>{parc}
<h2>Streets, parcels, EPC coverage</h2>{cov}
<h2>EPC built form</h2>{epcf}
<h2>TOID multiplicity</h2>{toid}
<h2>HMLR coverage</h2>{hm}
<h2>Map (sample)</h2>
<p>Sample: {len(geo["buildings"]["features"]):,} buildings, {len(geo["uprns"]["features"]):,} UPRNs,
{len(geo["toids"]["features"]):,} TOIDs,
{len(geo["parcels"]["features"]):,} INSPIRE parcels (QA only),
{len(geo["lines"]["features"]):,} disagreement lines (largest first).</p>
<p class=legend id=legend></p><div id=map></div>
<h2>Suspicious cases</h2>
<h3>Largest uprns_in_building</h3>{s1}
<h3>Disagreements with largest distance</h3>{s2}
<h3>UPRN with point but no building</h3>{s3}
<script src="{LEAFLET_JS}"></script>
<script>
const D={data}, C={colours};
const map=L.map('map');
L.tileLayer('https://tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png',
  {{maxZoom:19,attribution:'&copy; OpenStreetMap contributors'}}).addTo(map);
const b=L.geoJSON(D.buildings,{{style:{{color:'#555',weight:1,fillOpacity:.15}}}}).addTo(map);
const lines=L.geoJSON(D.lines,{{style:{{color:'#e41a1c',weight:2}},
  onEachFeature:(f,l)=>l.bindPopup('UPRN '+f.properties.uprn+'<br>'+f.properties.dist_m+' m')}}).addTo(map);
const toids=L.geoJSON(D.toids,{{pointToLayer:(f,ll)=>L.circleMarker(ll,{{radius:3,color:'#000',
  weight:1,fillColor:'#fff',fillOpacity:1}}),
  onEachFeature:(f,l)=>l.bindPopup('TOID '+f.properties.toid)}}).addTo(map);
const up=L.geoJSON(D.uprns,{{pointToLayer:(f,ll)=>L.circleMarker(ll,{{radius:4,weight:1,color:'#fff',
  fillColor:C[f.properties.method]||'#999',fillOpacity:.9}}),
  onEachFeature:(f,l)=>l.bindPopup('UPRN '+f.properties.uprn+'<br>'+f.properties.method+
  '<br>street: '+(f.properties.street||'-')+'<br>in building: '+f.properties.n_in_building+
  '<br>in parcel: '+(f.properties.n_in_parcel??'-')+'<br>outline: '+(f.properties.granularity||'-'))}}).addTo(map);
const parcels=L.geoJSON(D.parcels,{{style:{{color:'#1f78b4',weight:1,fill:false,dashArray:'3'}}}});
L.control.layers(null,{{Buildings:b,'INSPIRE parcels (QA)':parcels,'UPRNs':up,
  'TOID points':toids,'Disagreement lines':lines}}).addTo(map);
const bb=up.getBounds().isValid()?up.getBounds():b.getBounds();
if(bb.isValid())map.fitBounds(bb);else map.setView([54,-2],6);
document.getElementById('legend').innerHTML=Object.entries(C).map(([k,v])=>
  '<span><i class=sw style="background:'+v+'"></i>'+k+'</span>').join('')+
  '<span><i class=sw style="background:#fff;border:1px solid #000"></i>TOID point</span>'+
  '<span style="color:#e41a1c">&#9473; disagree</span>';
</script></body></html>
"""


def qa(lad: str, out_dir: Path, report_path: Path, staged_dir: Path | None = None) -> dict:
    _lad(lad)
    out = Path(out_dir) / f"lad={lad}"
    if not out.exists() and (Path(out_dir) / lad).exists():  # legacy layout
        out = Path(out_dir) / lad
    stats = json.loads((out / "stats.json").read_text())
    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")
    # Only non-address columns are read.
    have = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet({_q(out / 'uprn.parquet')})").fetchall()}
    opt = ", ".join(
        c if c in have else f"NULL AS {c}"
        for c in ("street_name", "outline_granularity", "uprns_in_parcel", "parcel_inspire_id")
    )
    con.execute(f"""
        CREATE VIEW u0 AS SELECT uprn, x, y, toid, toid_count, toid_x, toid_y, building_id,
               match_method, methods_disagree, uprns_in_building, {opt}
        FROM read_parquet({_q(out / "uprn.parquet")})""")
    staged_poly = (
        (Path(staged_dir) if staged_dir else Path(out_dir).parent / "staged") / lad / "inspire_polygons.parquet"
    )
    has_parcels = staged_poly.exists()
    if has_parcels:
        con.execute(f"CREATE VIEW parcels AS SELECT parcel_inspire_id, geom_wkb FROM read_parquet({_q(staged_poly)})")
    con.execute("""
        CREATE TEMP TABLE u AS SELECT *,
          CASE WHEN toid_x IS NOT NULL AND x IS NOT NULL
               THEN sqrt((x - toid_x)^2 + (y - toid_y)^2) END AS dist FROM u0""")
    con.execute(f"CREATE VIEW bld AS SELECT building_id, geometry FROM read_parquet({_q(out / 'buildings.parquet')})")

    toid_mult = con.execute("""
        SELECT CASE WHEN toid_count = 0 THEN '0 (no TOID)' WHEN toid_count = 1 THEN '1'
                    WHEN toid_count = 2 THEN '2' ELSE '3+' END AS k, count(*)
        FROM u GROUP BY 1 ORDER BY 1""").fetchall()
    max_dist = con.execute("SELECT max(dist) FROM u WHERE methods_disagree").fetchone()[0]
    geo = _collect(con, stats, has_parcels)
    susp = _suspicious(con)
    con.close()

    page = _render(stats, {"toid_mult": toid_mult}, susp, geo)
    report_path = Path(report_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(page, encoding="utf-8")
    return {
        "lad": lad,
        "report_path": str(report_path),
        "total_uprns": stats["total_uprns"],
        "match_method": stats["match_method"],
        "methods_disagree": stats["methods_disagree"],
        "toid_multiplicity": {k: n for k, n in toid_mult},
        "max_disagreement_dist_m": _round(max_dist),
        "sample": {
            "buildings": len(geo["buildings"]["features"]),
            "uprns": len(geo["uprns"]["features"]),
            "toids": len(geo["toids"]["features"]),
            "lines": len(geo["lines"]["features"]),
            "parcels": len(geo["parcels"]["features"]),
        },
        "outline_granularity": stats.get("outline_granularity"),
        "with_street_name": stats.get("with_street_name"),
        "with_parcel": stats.get("with_parcel"),
        "with_epc": stats.get("with_epc"),
        "suspicious_counts": {k: len(v) for k, v in susp.items()},
        "suspicious": susp,
    }
