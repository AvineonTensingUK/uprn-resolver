# Upstream data anomalies: evidence pack

Generated 2026-10-23 10:21:17 by `uv run python -m uprn_resolver.anomalies` over `data/parquet/*`. Per-anomaly CSVs
(identifiers and numeric measures only) are in `data/anomalies/`. No address text or postcodes from restricted sources appear
here or in the CSVs. Nothing in this pack has been sent to anyone.

Caveats: counts are candidates for publisher review, not proven errors. `severity` is our judgement of downstream harm.
The local Open TOID Parquet is filtered to TOIDs present in LIDS at conversion time, so `lids_toid_missing_from_open_toid`
measures LIDS TOIDs absent from every Open TOID tile we hold (0 means none). Open Names covers named roads only. Code-Point
is the only open source of postcode centroids (ONSUD has none). EPC and Price Paid ids are used as identifiers only.
Scotland is excluded from the EPC postcode-distance check because the Scottish register carries no usable UPRN column in our data.

## Headline

| Anomaly | Count | % of base |
|---|---:|---:|
| UPRNs in ONSUD that have no point in OS Open UPRN | 5,814 | 0.0140% |
| UPRNs in OS Open UPRN that are absent from ONSUD | 89,262 | 0.2142% |
| OS Open UPRN rows with no usable coordinates | 0 | 0.0000% |
| UPRN point isolated from every other UPRN of its ONSUD local authority | 7 | 0.0000% |
| UPRN that is the only one of its ONSUD LAD within 2 km but sits among 100+ UPRNs of other LADs | 5 | 0.0000% |
| LIDS UPRN to TopographicArea TOID link where the Open TOID point is > 100 m from the UPRN | 545,834 | 1.3110% |
| LIDS UPRN to USRN link where the UPRN is > 250 m from the Open USRN geometry | 1,099,701 | 2.5937% |
| USRNs referenced by LIDS that are missing from OS Open USRN | 747 | 0.0484% |
| Road TOIDs in LIDS (Road_TOID_Street_USRN_10) with no OS Open Names entry | 5,207 | 0.6146% |
| TopographicArea TOIDs referenced by LIDS that are missing from OS Open TOID | 0 | 0.0000% |
| Price Paid UPRN lookup UPRNs not present in ONSUD | 8 | 0.0095% |
| Price Paid INSPIRE ID lookup values not found in the INSPIRE Index Polygons | 3,107 | 4.1977% |
| EPC certificate UPRNs that do not exist in ONSUD | 4,523 | 0.0198% |
| EPC records whose UPRN point is > 5 km from the centroid of the EPC postcode | 874 | 0.0042% |
| ONSUD postcodes absent from Code-Point Open | 1,490 | 0.0858% |

## Ordnance Survey (Open UPRN, Open USRN, Open TOID, Open Names, Linked Identifiers, Code-Point Open)

| Anomaly | Count | % of base | Severity | Example ids | CSV |
|---|---:|---:|---|---|---|
| UPRNs in OS Open UPRN that are absent from ONSUD | 89,262 | 0.214% | Medium | `397444`, `397445`, `397446` | `open_uprn_not_in_onsud.csv` |
| OS Open UPRN rows with no usable coordinates | 0 | 0.0000% | Medium |  | `open_uprn_no_coordinates.csv` |
| LIDS UPRN to TopographicArea TOID link where the Open TOID point is > 100 m from the UPRN | 545,834 | 1.311% | Medium | `10095414208`, `10090635366`, `130167934` | `lids_topo_toid_far_from_uprn.csv` |
| LIDS UPRN to USRN link where the UPRN is > 250 m from the Open USRN geometry | 1,099,701 | 2.594% | Medium | `10024656737`, `10015398820`, `10024656713` | `uprn_far_from_usrn_street.csv` |
| USRNs referenced by LIDS that are missing from OS Open USRN | 747 | 0.048% | Medium | `20602353`, `7788083`, `33432545` | `usrn_missing_from_open_usrn.csv` |
| Road TOIDs in LIDS (Road_TOID_Street_USRN_10) with no OS Open Names entry | 5,207 | 0.615% | Low | `osgb4000000003210681`, `osgb4000000003210682`, `osgb4000000003210683` | `road_toid_missing_from_open_names.csv` |
| TopographicArea TOIDs referenced by LIDS that are missing from OS Open TOID | 0 | 0.0000% | High |  | `lids_toid_missing_from_open_toid.csv` |
| ONSUD postcodes absent from Code-Point Open | 1,490 | 0.086% | Low |  | `count only` |

### UPRNs in OS Open UPRN that are absent from ONSUD

- Count: 89,262 of 41,676,575 Open UPRN UPRNs.
- Examples: `397444`, `397445`, `397446`, `397447`, `397448`
- Method: Anti-join Open UPRN.uprn against ONSUD.uprn.
- Suggested fix: Align the AddressBase extract date of Open UPRN and ONSUD, or flag in the ONSUD release notes the UPRNs that postdate its cut-off.

### OS Open UPRN rows with no usable coordinates

- Count: 0 of 41,676,575 Open UPRN rows.
- Examples: 
- Method: Open UPRN rows where x, y, lat or lon is NULL, or (x, y) = (0, 0).
- Suggested fix: Drop such rows from the CSV or state in the product guide that they exist; where the coordinate is known to GeoPlace, supply it.

### LIDS UPRN to TopographicArea TOID link where the Open TOID point is > 100 m from the UPRN

- Count: 545,834 of 41,633,824 LIDS links with both points.
- over 1km: 9,340
- over 10km: 113
- over 100km: 0
- Examples: `10095414208`, `10090635366`, `130167934`, `125054392`, `122061821`
- Method: LIDS BLPU_UPRN_TopographicArea_TOID_5 joined to Open UPRN (point) and Open TOID (point guaranteed inside the MasterMap feature); Euclidean distance > 100 m. Large features (parks, estates) legitimately exceed 100 m; the >1 km and >10 km tiers are the real errors.
- Suggested fix: Re-check the UPRN to TOID association for the >1 km tier (likely a wrong TOID or UPRN position); document the expected tolerance for large features in the LIDS guide.

### LIDS UPRN to USRN link where the UPRN is > 250 m from the Open USRN geometry

- Count: 1,099,701 of 42,398,324 UPRN-to-USRN links with a point and a street geometry.
- over 1km: 156,294
- over 10km: 7,707
- Examples: `10024656737`, `10015398820`, `10024656713`, `10024924269`, `10024924271`
- Method: LIDS BLPU_UPRN_Street_USRN_11 joined to Open UPRN points and Open USRN lines; distance from the point to the line > 250 m (bbox shortcut for the clearly far, exact ST_Distance otherwise).
- Suggested fix: Review the UPRN to street association for the far links (many are the wrong USRN being the nearest named street rather than the access street); consider a published tolerance/quality flag on the link.

### USRNs referenced by LIDS that are missing from OS Open USRN

- Count: 747 of 1,543,152 distinct USRNs in LIDS street links.
- uprn links affected: 11,876
- Examples: `20602353`, `7788083`, `33432545`, `41009041`, `2753825`
- Method: Anti-join the USRNs of BLPU_UPRN_Street_USRN_11 and Road_TOID_Street_USRN_10 against Open USRN.usrn; counts of affected links kept.
- Suggested fix: Publish Open USRN and LIDS from the same epoch, or state which USRN types (e.g. private, unadopted, Scottish) Open USRN omits.

### Road TOIDs in LIDS (Road_TOID_Street_USRN_10) with no OS Open Names entry

- Count: 5,207 of 847,161 distinct road TOIDs in LIDS.
- named in open roads: 0
- Examples: `osgb4000000003210681`, `osgb4000000003210682`, `osgb4000000003210683`, `osgb4000000003210684`, `osgb4000000003210685`
- Method: Anti-join distinct Road_TOID_Street_USRN_10 road TOIDs against the Open Names Named Road TOID. `named_in_open_roads` marks those that carry a name in OS Open Roads, so the omission is not just an unnamed road.
- Suggested fix: Include the named-in-Open-Roads cases in Open Names (or explain the omission rule); unnamed roads are expected to be absent.

### TopographicArea TOIDs referenced by LIDS that are missing from OS Open TOID

- Count: 0 of 29,609,826 distinct TOIDs in LIDS BLPU_UPRN_TopographicArea_TOID_5.
- uprns affected: 0
- Examples: 
- Method: Anti-join LIDS.toid against Open TOID.toid (all 54 grid-square files).
- Suggested fix: Add the missing TOIDs to Open TOID or note why they are excluded (e.g. TOIDs retired since the LIDS cut); a missing 100 km tile would show as a block.

### ONSUD postcodes absent from Code-Point Open

- Count: 1,490 of 1,735,815 distinct ONSUD postcodes.
- uprns affected: 7,554
- Examples: 
- Method: Normalise ONSUD.postcode and Code-Point Open postcode (upper, single space before the 3-character inward code) and anti-join. Count only: no CSV.
- Suggested fix: Expected for postcodes created or terminated between the two releases; align release dates or document the lag.

### Where to report

- OS Data Hub errors-and-omissions reporting tool (needs a free OS Data Hub account): https://osdatahub.os.uk/apps/errors-and-omissions/reporting
- OS report-a-data-error page (also phone 03453 757535, Mon-Fri 8-6): https://www.ordnancesurvey.co.uk/contact-us/business-government/report-error
- Check known issues first: https://docs.os.uk/os-downloads/identifiers/os-open-uprn/os-open-uprn-known-data-issues and https://docs.os.uk/os-downloads/identifiers/os-open-usrn/os-open-usrn-known-data-issues

### Covering note (draft, not sent)

```
To: OS Data Hub support (via the errors-and-omissions tool)
Subject: Possible inconsistencies found when joining the open datasets (evidence attached)

Hello,

While building an open UPRN-to-building lookup for Great Britain we cross-checked the open releases against
each other and found the inconsistencies below. Each is reproducible with set-based joins on identifiers only;
the attached CSVs contain UPRN / TOID / USRN / INSPIRE ids and numeric measures, with no address text.

- Open UPRN / ONSUD set differences and UPRNs without coordinates.
- LIDS UPRN to TOID links where the Open TOID point is far from the UPRN.
- LIDS UPRN to USRN links far from the Open USRN line; LIDS USRNs missing from Open USRN.
- Road TOIDs in LIDS with no Open Names entry; ONSUD postcodes missing from Code-Point Open.

We are happy to share the exact queries (uprn_resolver/anomalies.py) and to re-run them against a corrected release.
Please tell us if any of these are known and intended.

Kind regards,
[name]
```

## GeoPlace (custodian of the UPRN/USRN address and street gazetteers; upstream of OS Open UPRN, Open USRN and LIDS)

Also relevant (the anomalies below are listed under their publishing owner above; copy this body for routing).

| Anomaly | Count | % of base | Severity | Example ids | CSV |
|---|---:|---:|---|---|---|
| UPRN point isolated from every other UPRN of its ONSUD local authority | 7 | 0.0000% | High | `10093645993`, `9051159397`, `130200314` | `uprn_far_from_onsud_lad.csv` |
| UPRN that is the only one of its ONSUD LAD within 2 km but sits among 100+ UPRNs of other LADs | 5 | 0.0000% | High | `24042771`, `9051159397`, `10023823835` | `uprn_inside_other_lad_territory.csv` |
| LIDS UPRN to TopographicArea TOID link where the Open TOID point is > 100 m from the UPRN | 545,834 | 1.311% | Medium | `10095414208`, `10090635366`, `130167934` | `lids_topo_toid_far_from_uprn.csv` |
| LIDS UPRN to USRN link where the UPRN is > 250 m from the Open USRN geometry | 1,099,701 | 2.594% | Medium | `10024656737`, `10015398820`, `10024656713` | `uprn_far_from_usrn_street.csv` |
| USRNs referenced by LIDS that are missing from OS Open USRN | 747 | 0.048% | Medium | `20602353`, `7788083`, `33432545` | `usrn_missing_from_open_usrn.csv` |

### UPRN point isolated from every other UPRN of its ONSUD local authority

- Count: 7 of 41,587,313 UPRNs with a point and an ONSUD LAD.
- over 100km from lad median: 3
- over 300km from lad median: 1
- Examples: `10093645993`, `9051159397`, `130200314`, `130194606`, `130165988`
- Method: For each ONSUD LAD, bin Open UPRN points into 10 km cells; flag UPRNs whose cell and the surrounding +-2 cells hold at most 3 UPRNs of that LAD, i.e. nothing else of the LAD within ~10 km. Catches a point near Cambridge for a Torridge UPRN. Remote islands and bothies can be genuine; check the distance to the LAD median.
- Suggested fix: Either the ONSUD LAD or the Open UPRN coordinate is wrong for these UPRNs. Re-derive the ONSUD geography from the Open UPRN point (ONSUD is spatially joined from it) or correct the coordinate at source (GeoPlace/local authority).

### UPRN that is the only one of its ONSUD LAD within 2 km but sits among 100+ UPRNs of other LADs

- Count: 5 of 41,587,313 UPRNs with a point and an ONSUD LAD.
- Examples: `24042771`, `9051159397`, `10023823835`, `10093645993`, `10010317812`
- Method: Bin Open UPRN points into 1 km cells; flag a UPRN when no other UPRN of its ONSUD LAD lies in the surrounding +-2 cell window while the window holds at least 100 UPRNs of other LADs. A point deep inside another authority is a wrong LAD or a wrong coordinate (genuine exclaves are rare; boundary UPRNs are not flagged).
- Suggested fix: Re-derive ONSUD geography from the Open UPRN point, or correct the coordinate at source; publish a point-in-LAD consistency check with the directory.

### LIDS UPRN to TopographicArea TOID link where the Open TOID point is > 100 m from the UPRN

- Count: 545,834 of 41,633,824 LIDS links with both points.
- over 1km: 9,340
- over 10km: 113
- over 100km: 0
- Examples: `10095414208`, `10090635366`, `130167934`, `125054392`, `122061821`
- Method: LIDS BLPU_UPRN_TopographicArea_TOID_5 joined to Open UPRN (point) and Open TOID (point guaranteed inside the MasterMap feature); Euclidean distance > 100 m. Large features (parks, estates) legitimately exceed 100 m; the >1 km and >10 km tiers are the real errors.
- Suggested fix: Re-check the UPRN to TOID association for the >1 km tier (likely a wrong TOID or UPRN position); document the expected tolerance for large features in the LIDS guide.

### LIDS UPRN to USRN link where the UPRN is > 250 m from the Open USRN geometry

- Count: 1,099,701 of 42,398,324 UPRN-to-USRN links with a point and a street geometry.
- over 1km: 156,294
- over 10km: 7,707
- Examples: `10024656737`, `10015398820`, `10024656713`, `10024924269`, `10024924271`
- Method: LIDS BLPU_UPRN_Street_USRN_11 joined to Open UPRN points and Open USRN lines; distance from the point to the line > 250 m (bbox shortcut for the clearly far, exact ST_Distance otherwise).
- Suggested fix: Review the UPRN to street association for the far links (many are the wrong USRN being the nearest named street rather than the access street); consider a published tolerance/quality flag on the link.

### USRNs referenced by LIDS that are missing from OS Open USRN

- Count: 747 of 1,543,152 distinct USRNs in LIDS street links.
- uprn links affected: 11,876
- Examples: `20602353`, `7788083`, `33432545`, `41009041`, `2753825`
- Method: Anti-join the USRNs of BLPU_UPRN_Street_USRN_11 and Road_TOID_Street_USRN_10 against Open USRN.usrn; counts of affected links kept.
- Suggested fix: Publish Open USRN and LIDS from the same epoch, or state which USRN types (e.g. private, unadopted, Scottish) Open USRN omits.

### Where to report

- support@geoplace.co.uk (UPRN integration/support contact in GeoPlace's published checklist: https://static.geoplace.co.uk/downloads/UPRNIntegrationChecklist.pdf)
- Corrections to a UPRN position or street link are made by the owning local authority via GeoPlace's data-improvement process; send the id lists for the UPRN/USRN anomalies and ask for routing.

### Covering note (draft, not sent)

```
To: GeoPlace support
Subject: Possible inconsistencies found when joining the open datasets (evidence attached)

Hello,

While building an open UPRN-to-building lookup for Great Britain we cross-checked the open releases against
each other and found the inconsistencies below. Each is reproducible with set-based joins on identifiers only;
the attached CSVs contain UPRN / TOID / USRN / INSPIRE ids and numeric measures, with no address text.

- UPRNs whose point lies far from their local authority (e.g. a Torridge UPRN positioned near Cambridge).
- UPRN to USRN and UPRN to TOID links where the UPRN is far from the street / feature.
- USRNs referenced by the links but absent from Open USRN.

We are happy to share the exact queries (uprn_resolver/anomalies.py) and to re-run them against a corrected release.
Please tell us if any of these are known and intended.

Kind regards,
[name]
```

## Office for National Statistics (ONS UPRN Directory, ONSUD)

| Anomaly | Count | % of base | Severity | Example ids | CSV |
|---|---:|---:|---|---|---|
| UPRNs in ONSUD that have no point in OS Open UPRN | 5,814 | 0.014% | Medium | `341799`, `388030`, `388031` | `onsud_uprn_not_in_open_uprn.csv` |
| UPRN point isolated from every other UPRN of its ONSUD local authority | 7 | 0.0000% | High | `10093645993`, `9051159397`, `130200314` | `uprn_far_from_onsud_lad.csv` |
| UPRN that is the only one of its ONSUD LAD within 2 km but sits among 100+ UPRNs of other LADs | 5 | 0.0000% | High | `24042771`, `9051159397`, `10023823835` | `uprn_inside_other_lad_territory.csv` |

### UPRNs in ONSUD that have no point in OS Open UPRN

- Count: 5,814 of 41,593,127 ONSUD UPRNs.
- Examples: `341799`, `388030`, `388031`, `5206239`, `5206240`
- Method: Anti-join ONSUD.uprn against Open UPRN.uprn (same release window).
- Suggested fix: Publish the ONSUD UPRN set and Open UPRN from the same AddressBase epoch, or document which UPRNs (e.g. non-geographic, historic) ONSUD carries that Open UPRN omits.

### UPRN point isolated from every other UPRN of its ONSUD local authority

- Count: 7 of 41,587,313 UPRNs with a point and an ONSUD LAD.
- over 100km from lad median: 3
- over 300km from lad median: 1
- Examples: `10093645993`, `9051159397`, `130200314`, `130194606`, `130165988`
- Method: For each ONSUD LAD, bin Open UPRN points into 10 km cells; flag UPRNs whose cell and the surrounding +-2 cells hold at most 3 UPRNs of that LAD, i.e. nothing else of the LAD within ~10 km. Catches a point near Cambridge for a Torridge UPRN. Remote islands and bothies can be genuine; check the distance to the LAD median.
- Suggested fix: Either the ONSUD LAD or the Open UPRN coordinate is wrong for these UPRNs. Re-derive the ONSUD geography from the Open UPRN point (ONSUD is spatially joined from it) or correct the coordinate at source (GeoPlace/local authority).

### UPRN that is the only one of its ONSUD LAD within 2 km but sits among 100+ UPRNs of other LADs

- Count: 5 of 41,587,313 UPRNs with a point and an ONSUD LAD.
- Examples: `24042771`, `9051159397`, `10023823835`, `10093645993`, `10010317812`
- Method: Bin Open UPRN points into 1 km cells; flag a UPRN when no other UPRN of its ONSUD LAD lies in the surrounding +-2 cell window while the window holds at least 100 UPRNs of other LADs. A point deep inside another authority is a wrong LAD or a wrong coordinate (genuine exclaves are rare; boundary UPRNs are not flagged).
- Suggested fix: Re-derive ONSUD geography from the Open UPRN point, or correct the coordinate at source; publish a point-in-LAD consistency check with the directory.

### Where to report

- ons.geography@ons.gov.uk (ONS Geography enquiries address listed on each ONSUD release at data.gov.uk, e.g. https://www.data.gov.uk/dataset/dc27178b-62b0-4fac-a2dc-11959a999914/ons-uprn-directory-march-2024-user-guide)

### Covering note (draft, not sent)

```
To: ONS Geography (ons.geography@ons.gov.uk)
Subject: Possible inconsistencies found when joining the open datasets (evidence attached)

Hello,

While building an open UPRN-to-building lookup for Great Britain we cross-checked the open releases against
each other and found the inconsistencies below. Each is reproducible with set-based joins on identifiers only;
the attached CSVs contain UPRN / TOID / USRN / INSPIRE ids and numeric measures, with no address text.

- UPRNs that are the only one of their ONSUD LAD within 2 km but sit among 100+ UPRNs of other LADs.
- UPRNs isolated by 10+ km from the rest of their ONSUD LAD (e.g. 10093645993: ONSUD Torridge, point near Cambridge).
- ONSUD vs Open UPRN set differences.

We are happy to share the exact queries (uprn_resolver/anomalies.py) and to re-run them against a corrected release.
Please tell us if any of these are known and intended.

Kind regards,
[name]
```

## HM Land Registry (Price Paid UPRN / INSPIRE ID lookups, INSPIRE Index Polygons)

| Anomaly | Count | % of base | Severity | Example ids | CSV |
|---|---:|---:|---|---|---|
| Price Paid UPRN lookup UPRNs not present in ONSUD | 8 | 0.0095% | Low | `{1557763C-E35D-45C6-A986-9DD2F0D2F7E7}`, `{3DCCB7CA-AE1A-5B9D-E063-4704A8C0331E}`, `{446579F5-BC42-4C0F-8665-56587B257AF9}` | `hmlr_pp_uprn_not_in_onsud.csv` |
| Price Paid INSPIRE ID lookup values not found in the INSPIRE Index Polygons | 3,107 | 4.198% | Medium | `{0F5CBBC5-05FB-44BD-9983-C88F97FA3469}`, `{0F5CBBC5-05FB-44BD-9983-C88F97FA3469}`, `{0F5CBBC5-05FB-44BD-9983-C88F97FA3469}` | `hmlr_pp_inspire_not_in_polygons.csv` |

### Price Paid UPRN lookup UPRNs not present in ONSUD

- Count: 8 of 84,149 Price Paid UPRN lookup rows with a numeric UPRN.
- Examples: `{1557763C-E35D-45C6-A986-9DD2F0D2F7E7}`, `{3DCCB7CA-AE1A-5B9D-E063-4704A8C0331E}`, `{446579F5-BC42-4C0F-8665-56587B257AF9}`, `{5A7B802C-BD74-1886-E063-4704A8C021AE}`, `{5A7B802D-75BD-1886-E063-4704A8C021AE}`
- Method: Anti-join pp-uprn-lookup.value (cast to BIGINT) against ONSUD.uprn.
- Suggested fix: Confirm the UPRN source epoch of the lookup and the ONSUD/AddressBase vintage; drop or flag UPRNs that are retired/non-geographic.

### Price Paid INSPIRE ID lookup values not found in the INSPIRE Index Polygons

- Count: 3,107 of 74,017 Price Paid INSPIRE lookup rows.
- Examples: `{0F5CBBC5-05FB-44BD-9983-C88F97FA3469}`, `{0F5CBBC5-05FB-44BD-9983-C88F97FA3469}`, `{0F5CBBC5-05FB-44BD-9983-C88F97FA3469}`, `{1050E854-4F43-43D5-B77F-85C5FD073DDC}`, `{1D817EC0-A4F1-458C-968E-565BED342E86}`
- Method: Anti-join pp-inspire-id-lookup.value against the inspire_id of all INSPIRE Index Polygon council files.
- Suggested fix: Release the lookup and the INSPIRE polygons from the same snapshot, or document that parcels are removed (title closures, merges) without back-filling the lookup.

### Where to report

- HMLR Data Services Team: data.services@mail.landregistry.gov.uk, 0300 006 0478 (Mon-Fri 9-5), contact form linked from https://www.gov.uk/government/statistical-data-sets/transaction-unique-identifier-and-inspire-id-look-up-table-dataset
- INSPIRE Index Polygons guidance: https://www.gov.uk/inspire-index-polygons-spatial-data

### Covering note (draft, not sent)

```
To: HM Land Registry Data Services Team
Subject: Possible inconsistencies found when joining the open datasets (evidence attached)

Hello,

While building an open UPRN-to-building lookup for Great Britain we cross-checked the open releases against
each other and found the inconsistencies below. Each is reproducible with set-based joins on identifiers only;
the attached CSVs contain UPRN / TOID / USRN / INSPIRE ids and numeric measures, with no address text.

- Price Paid INSPIRE ID lookup values that are not present in the INSPIRE Index Polygons.
- Price Paid UPRN lookup UPRNs that are not in the ONS UPRN Directory.

We are happy to share the exact queries (uprn_resolver/anomalies.py) and to re-run them against a corrected release.
Please tell us if any of these are known and intended.

Kind regards,
[name]
```

## MHCLG (Energy Performance of Buildings Register)

| Anomaly | Count | % of base | Severity | Example ids | CSV |
|---|---:|---:|---|---|---|
| EPC certificate UPRNs that do not exist in ONSUD | 4,523 | 0.020% | Medium | `19282`, `318279`, `323051` | `epc_uprn_not_in_onsud.csv` |
| EPC records whose UPRN point is > 5 km from the centroid of the EPC postcode | 874 | 0.0042% | Medium | `24128559`, `310017757`, `10013261961` | `epc_uprn_far_from_postcode_centroid.csv` |

### EPC certificate UPRNs that do not exist in ONSUD

- Count: 4,523 of 22,821,474 distinct EPC UPRNs (E&W + Scotland).
- Examples: `19282`, `318279`, `323051`, `323052`, `338389`
- Method: Anti-join distinct (UPRN > 0) from EPC certificates (England & Wales domestic and non-domestic; Scotland) against ONSUD.uprn. No address field is read.
- Suggested fix: Validate the lodged UPRN against AddressBase at lodgement (the register accepts free-text UPRNs); correct or null the invalid ones in the bulk data.

### EPC records whose UPRN point is > 5 km from the centroid of the EPC postcode

- Count: 874 of 20,829,224 distinct EPC (England & Wales) UPRNs with a point and a Code-Point postcode.
- over 50km: 272
- Examples: `24128559`, `310017757`, `10013261961`, `42032646`, `10024067603`
- Method: EPC E&W certificates give (UPRN, postcode); the postcode is normalised and looked up in Code-Point Open (centroid) and compared with the Open UPRN point entirely inside DuckDB. UPRNs whose every certificate postcode is > 5 km away are kept (ids and distance only; no postcode leaves the database). ONSUD carries no coordinates, so Code-Point is the centroid source.
- Suggested fix: Cross-check lodged UPRN against postcode at lodgement; these are almost certainly a mistyped UPRN or postcode.

### Where to report

- mhclg.digital-services@communities.gov.uk (enquiries about the data; use subject prefix 'EPB - '), service page https://get-energy-performance-data.communities.gov.uk/
- Service feedback form linked from the same page.

### Covering note (draft, not sent)

```
To: MHCLG EPB digital services (subject: EPB - UPRN data quality)
Subject: Possible inconsistencies found when joining the open datasets (evidence attached)

Hello,

While building an open UPRN-to-building lookup for Great Britain we cross-checked the open releases against
each other and found the inconsistencies below. Each is reproducible with set-based joins on identifiers only;
the attached CSVs contain UPRN / TOID / USRN / INSPIRE ids and numeric measures, with no address text.

- EPC certificates whose UPRN does not exist in the ONS UPRN Directory.
- Certificates whose UPRN point is more than 5 km from the centroid of the lodged postcode.

We are happy to share the exact queries (uprn_resolver/anomalies.py) and to re-run them against a corrected release.
Please tell us if any of these are known and intended.

Kind regards,
[name]
```
