# Publishing to Source Cooperative

Research notes (checked 2026-10) and runbook. Source is in beta; details may change, so confirm against the docs.

## Findings

- **Accounts.** Browsing/downloading public data needs no account. Publishing needs a login (GitHub/Google) at
  source.coop, a username (this becomes the namespace in URLs), and approval as a beta tester via the application form.
  Creating an account does not grant upload rights; email hello@source.coop. [Create an account](https://docs.source.coop/create-an-account)
- **Organisations.** Create an individual account first, then email hello@source.coop to request an organisational
  account (roles: Owner, Administrator, Member). Product roles: Owner, Contributor, Viewer. [Core concepts](https://docs.source.coop/core-concepts)
- **Products (repositories).** Created in the web UI (top-right menu once approved). Fields: title (<=200 chars), owner
  account, product ID (3-39 chars, letters/digits/hyphens, no leading/trailing/consecutive hyphens; appears in the URL),
  description (<=500 chars), up to 20 tags, data connection (Source-managed storage by default; us-west-2 recommended).
  New products are Unlisted; set to Listed via the gear icon to appear in search. [Create a data product](https://docs.source.coop/create-a-data-product)
- **README.** The root `README.md` is rendered as the product landing page; include contact info and, per the core-concepts
  page, licence information (separately for metadata and data). Example: [ordnancesurvey/ngd-boundaries](https://source.coop/ordnancesurvey/ngd-boundaries)
  (OGL v3 stated in README + `licence.txt` at the root, a `GeoParquet/` folder, tags `opendata, geoparquet`).
  There is no separate licence field documented; licence goes in README/LICENCE files.
- **Upload.** S3-compatible. Endpoint `https://data.source.coop` (the data proxy); destination `s3://<account>/<product>/`;
  uploads outside the product prefix are refused. Credentials come from Source (not AWS), AWS-format with a session token,
  temporary, scoped to the product. Get them on the product page: lock icon > View Credentials > Environment Variables.
  Web UI upload exists for small files; a Source CLI (`source-coop login`) and service accounts (for CI) are mentioned; for
  long-term automated access email hello@source.coop. Some guides use the bucket endpoint `s3://us-west-2.opendata.source.coop/<account>/<product>/`
  instead; use whichever your credentials were issued for. [Upload your data](https://docs.source.coop/data-upload),
  [Data proxy](https://docs.source.coop/data-proxy), [community example](https://raw.githubusercontent.com/developmentseed/astera-demogorgn/refs/heads/main/docs/data-upload.md)
- **Reading.** `aws s3 ls s3://<account>/<product>/ --endpoint-url https://data.source.coop --no-sign-request`; HTTP at
  `https://data.source.coop/<account>/<product>/<path>`.
- **Size.** No practical per-object limit; products reach 100 TB+. Not a concern here.
- **Versioning.** No object versioning; product versioning is "planned". Convention: publish fully baked releases, so we use
  a top-level `<release>/` folder (e.g. `2026-10/`) per release. Releases are immutable here too: `assemble` refuses to
  rebuild an existing release folder without `force`, each release carries `release_manifest.json` (sizes, sha256, row
  counts, source versions) and optionally `CHANGELOG.json/.md`, and a root `latest.json` names the newest release.
- **Cloud-native practice.** GeoParquet 1.1 with bbox covering, Hilbert order, 50k-150k row groups, spatial partitions
  above ~2 GB ([distributing GeoParquet](https://github.com/opengeospatial/geoparquet/blob/main/format-specs/distributing-geoparquet.md));
  a static STAC catalogue for discovery ([STAC](https://stacspec.org/), table extension) which Source's plain HTTPS/S3
  hosting serves unchanged; national files optional (`--national-files`) for readers wanting few requests.

Not found in docs: exact CLI install/commands, service-account setup. Ask hello@source.coop.

## Release layout (built by `uprn_resolver.publish`)

Hive-partitioned, one file per table per LAD (see `docs/format.md`):

```
<product>/
  latest.json                       {"latest": "<release>", manifest, stac}; uploaded last
  README.md  LICENCE.md            (product root; copy latest release's to root manually/by re-sync)
  <release>/
    README.md  LICENCE.md  stats.json  lad_index.parquet  release_manifest.json  [CHANGELOG.json CHANGELOG.md]
    stac/catalog.json  stac/<table>/collection.json  stac/<table>/<lad>/<lad>.json   (static STAC, relative links)
    national/<table>/part-NNNN.parquet      only with --national-files
    uprn/lad=<code>/uprn.parquet            GeoParquet 1.1, Hilbert, bbox covering   (OGL)
    buildings/lad=<code>/buildings.parquet  GeoParquet 1.1, Hilbert, bbox covering   (OGL)
    epc/  provenance/                       plain Parquet                            (OGL)
    poi/                                    plain Parquet + LICENCE.txt              (Overture, per provider)
    streets_odbl/                           plain Parquet + LICENCE.txt              (ODbL-1.0, kept apart)
```

`epc`, `provenance`, `poi` and `streets_odbl` are included only when built. `address_local` and `resolved_local`
are never read. `resolved_open`, `outline_odbl` (ODbL) and `outline_ms` (CDLA-Permissive-2.0) are not yet assembled.

## Runbook

Manual steps (cannot be automated):
1. Sign in at source.coop, choose a username; submit the beta tester application; wait for approval.
2. (Optional) email hello@source.coop for an organisation account and for upload rights if "View Credentials" is missing.
3. Create the product in the UI (ID e.g. `uk-uprn-open`, tags `opendata, uprn, geoparquet, ogl`); leave Unlisted.
4. On the product page copy the temporary credentials (Environment Variables) into your shell. Install the AWS CLI (`brew install awscli`).
5. Run a dry run, inspect, then the real upload:
   ```
   uv run uprn-resolver publish --release 2026-10 --dest s3://<account>/uk-uprn-open/            # dry run (default)
   uv run uprn-resolver publish --release 2026-10 --dest s3://<account>/uk-uprn-open/ --execute  # upload
   ```
   The release is staged in `data/release/<release>/` (`--stage-dir` to change); upload is `aws s3 sync` to the data proxy.
   Run `uv run uprn-resolver validate` first and check the report has no FAIL.
6. Verify: `aws s3 ls s3://<account>/uk-uprn-open/2026-10/ --endpoint-url https://data.source.coop --no-sign-request`, and a DuckDB query.
7. Copy the release README/LICENCE to the product root so the landing page renders, update the README example paths, then set the product to Listed.
8. Repeat per release with a new `<release>` folder. Credentials expire; refresh as needed.

Release options (`publish.assemble` / `publish.publish` keyword arguments; CLI flags requested, see below):
`force` (rebuild an existing release), `stac` (default on; pystac-validated, fails the assemble on a schema error or
dangling asset href), `national_files` (default off), `changelog_against=<previous release dir>`, `source_manifest=<data/parquet/manifest.json>`.
STAC and manifest can be added to an already assembled release without touching its parquet:
`stac.build_stac(root)`, `publish.write_manifest(root, release, source_manifest=...)`, `publish.write_latest(stage, release)`.

Validation before upload: GeoParquet 1.1 `geo` metadata with covering on `uprn` and `buildings`; no column matching
address/street/thoroughfare/building_name/saon/paon/locality/town/postcode in OGL files, other than the exempt
Open Names fields (`street_name`, `locality` etc.) and with postcode only if passed via `--allow`. `streets_odbl` has its own
rules and needs its `LICENCE.txt`. The licence of each folder must be stated in the product README.
