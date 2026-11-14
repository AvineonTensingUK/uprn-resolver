# Maintenance runbook

## Schedule

- **Every 6 weeks**: OS Open UPRN, Open Linked Identifiers and ONSUD refresh together (6-weekly). Run `refresh` after the
  ONSUD release appears.
- **Monthly**: run `uv run uprn-resolver hmlr-archive`. HMLR publishes Price Paid lookups as "current month" files that may be
  replaced or removed, and they are not back-dated. A missed month is a permanent gap. `refresh` also runs it.
- OSM, Microsoft footprints (irregular, last 2026-08-13) and Overture (monthly) only when you want them (`--with-extras`).

## Steps

```
uv run uprn-resolver refresh --release 2026-12 [--with-extras] [--pbf FILE] [--restage]
```

Runs download (core, hmlr, streets, epc, inspire, ros; extras if asked), `hmlr-archive`, `stage --all`, `build --all`,
extras (address, provenance, poi, osm, outlines, parcels), `resolve --all`, `national`, `validate` and a `publish` **dry run**. Real
upload stays a manual `publish --execute`.

Resumable: each completed step is recorded in `<data-dir>/refresh.state.json` (per release); re-run the same command after
a failure and finished steps are skipped (per-LAD loops also skip finished LADs). `--fresh` ignores the file. The file is
deleted when everything succeeds. Failed steps are listed in the summary and the exit status is 1.
Before a release: read the validate report (`data/validation/report.html`) and the national stats.

## Disk and time

Plan for about 60 GB free (raw downloads, national Parquet, staged, out, DuckDB temp). Keep `data/parquet/hmlr/` forever.
England and Wales EPC needs a manual GOV.UK One Login download (see the epc module); the refresh warns and carries on without it.

## Ownership and feedback

Owner: the dataset maintainer. Corrections and questions via the repository issues (channel to
be set when the dataset is published on source.coop; **TBD**). Licence questions: see [licence-decisions.md](licence-decisions.md).

## Adding a release

1. `refresh` as above with a new `--release YYYY-MM`.
2. `uv run uprn-resolver accuracy --release R` once the release is assembled, then `release-extras --release R
   --source-manifest data/parquet/manifest.json` so `accuracy.json` is copied in and listed in the manifest.
3. Update the year in [publish/LICENCE.md](../publish/LICENCE.md) attribution lines.
4. Re-check upstream licence pages marked unverified (Overture attribution, OS wording).
5. `publish --release R --dest s3://... ` dry run, review, then `--execute`.

## Known anomalies

- ONSUD and Open UPRN disagree for some UPRNs (e.g. 10093645993 is in Torridge by ONSUD but near Cambridge by its point).
- 5,814 ONSUD UPRNs have no Open UPRN point; 47,195 have no TOID.
- Price Paid lookups cover only August 2026 onward (0.2% of UPRNs).
- `building_id` is not stable across releases; buildings straddling two LADs appear in both.
- Three LADs (E07000046, E08000025, E08000035) have no Microsoft outline input in the resolve step.
- Open UPRN and ONSUD epochs can differ by a release; check `input_row_counts` in the national stats.
