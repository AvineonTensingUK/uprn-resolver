"""Download OS and ONS open datasets into ``raw_dir/<dataset>/``.

Downloads are untrusted data: they are streamed to disk, checksum-verified where
the source gives one, and unzipped (with a path-traversal guard) into the
dataset's own directory. Nothing from an archive is ever executed.

Flow per file: download -> extract -> ``convert()`` to Parquet -> row count verified ->
extracted files and zip deleted. Already-converted files are skipped via
``<parquet_dir>/manifest.json`` (plus our own ``downloads.json`` sidecar). Up to 3 downloads
run in parallel, but only one extract+convert runs at a time. Disk space is checked before
every download and extraction.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

OS_API = "https://api.os.uk/downloads/v1/products"
ARCGIS = "https://www.arcgis.com/sharing/rest"
DEFAULT_AREAS = ["all"]  # "all" = every GB tile; e.g. "TQ" covers Islington

# dataset -> (OS product id, format, optional file-name regex, tiled?)
OS_PRODUCTS = {
    "open_uprn": ("OpenUPRN", "CSV", None, False),
    "lids": ("LIDS", "CSV", r"BLPU-UPRN-TopographicArea-TOID", False),
    "open_toid": ("OpenTOID", "CSV", None, True),
    "openmap_local": ("OpenMapLocal", "ESRI® Shapefile", None, True),
}
ALL_DATASETS = ["open_uprn", "lids", "open_toid", "openmap_local", "onsud"]
TIMEOUT = (30, 120)
MIN_FREE_GB = 10
POLL_SECONDS = 60
MAX_WAIT_SECONDS = 30 * 60
DOWNLOAD_WORKERS = 3
_CONVERT_LOCK = threading.Lock()  # at most one extract+convert at a time
_MANIFEST_LOCK = threading.Lock()


class DiskSpaceError(RuntimeError):
    pass


def _free_bytes(path: Path) -> int:
    p = Path(path).resolve()
    while not p.exists():
        p = p.parent
    return shutil.disk_usage(p).free


def _wait_for_space(path: Path, need: int = 0, what: str = "") -> None:
    """Block until free space >= MIN_FREE_GB + need; raise DiskSpaceError after 30 min."""
    floor = MIN_FREE_GB * 1024**3 + need
    waited = 0
    while _free_bytes(path) < floor:
        if waited >= MAX_WAIT_SECONDS:
            raise DiskSpaceError(f"{what}: free disk below {floor / 1024**3:.1f} GB after 30 min")
        print(f"  low disk ({_free_bytes(path) / 1024**3:.1f} GB free), waiting: {what}")
        time.sleep(POLL_SECONDS)
        waited += POLL_SECONDS


def _wait_for_file(path: Path, what: str) -> None:
    waited = 0
    while not path.exists():
        if waited >= MAX_WAIT_SECONDS:
            raise RuntimeError(f"{what}: {path} did not appear within 30 min")
        print(f"  waiting for {path} ...")
        time.sleep(POLL_SECONDS)
        waited += POLL_SECONDS


def _json_load(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _walk(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def _already_converted(parquet_dir: Path, name: str, md5: str | None, key: str | None = None) -> bool:
    """True if manifest.json records this download (by zip name, md5 or converted parquet key)."""
    data = _json_load(parquet_dir / "manifest.json")
    if not isinstance(data, dict):
        return False
    if key and key in (data.get("converted") or {}):
        return True
    for k, v in (data.get("downloads") or {}).items():
        if k == name or (md5 and isinstance(v, dict) and str(v.get("md5", "")).lower() == md5.lower()):
            return True
    return False


def _record(parquet_dir: Path, dataset: str, name: str, size: int | None, md5: str | None, rows: int) -> None:
    """Record a converted download under manifest["downloads"] (same flock as convert())."""
    path = parquet_dir / "manifest.json"
    parquet_dir.mkdir(parents=True, exist_ok=True)
    with _MANIFEST_LOCK, open(parquet_dir / ".manifest.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = _json_load(path)
        if not isinstance(data, dict):
            data = {}
        data.setdefault("downloads", {})[name] = {
            "dataset": dataset,
            "size": size,
            "md5": md5,
            "rows": rows,
            "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        tmp = path.with_name(f"manifest.json.{os.getpid()}.dl.tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
        tmp.replace(path)


def _stream(url: str, dest: Path, size: int | None = None, md5: str | None = None) -> Path:
    """Stream url to dest (via .part), skipping if dest exists with the right size."""
    if dest.exists() and (size is None or dest.stat().st_size == size):
        print(f"  have {dest.name}, skipping")
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    _wait_for_space(dest.parent, size or 0, f"download {dest.name}")
    # partial file lives outside data/raw (something tidied .part files away mid-download)
    part = dest.parent.parent.parent / ".downloads" / dest.parent.name / (dest.name + ".part")
    part.parent.mkdir(parents=True, exist_ok=True)
    (part.parent / ".keep").touch()  # an empty-dir pruner elsewhere was removing our directory
    h = hashlib.md5()
    print(f"  downloading {dest.name} ...")
    with requests.get(url, stream=True, timeout=TIMEOUT, allow_redirects=True) as r:
        r.raise_for_status()
        with open(part, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
                h.update(chunk)
    got = part.stat().st_size
    if size is not None and got != size:
        part.unlink()
        raise OSError(f"{dest.name}: size {got} != expected {size}")
    if md5 and h.hexdigest() != md5.lower():
        part.unlink()
        raise OSError(f"{dest.name}: md5 mismatch")
    dest.parent.mkdir(parents=True, exist_ok=True)
    (dest.parent / ".keep").touch()
    part.rename(dest)
    return dest


def _unzip(zpath: Path, target: Path) -> list[Path]:
    """Extract zip into target, refusing members that escape it. Returns extracted files."""
    root = target.resolve()
    with zipfile.ZipFile(zpath) as z:
        infos = z.infolist()
        for m in infos:
            if not (root / m.filename).resolve().is_relative_to(root):
                raise OSError(f"unsafe path in {zpath.name}: {m.filename}")
        _wait_for_space(target, sum(m.file_size for m in infos), f"extract {zpath.name}")
        z.extractall(root)
    return [root / m.filename for m in infos if not m.is_dir()]


def _sources(dataset: str, files: list[Path]) -> list[Path]:
    """Pick the files to hand to convert() from an extracted archive."""
    if dataset == "openmap_local":  # Building layer only
        return sorted(f for f in files if f.suffix.lower() == ".shp" and re.search(r"(^|_)building$", f.stem, re.I))
    if dataset == "lids":
        return sorted(f for f in files if f.suffix.lower() == ".csv")
    if dataset == "onsud":  # one call with the directory of per-region CSVs
        csvs = [f for f in files if f.suffix.lower() == ".csv"]
        multi = [f for f in csvs if "multi_csv" in f.parts] or csvs
        return sorted({f.parent for f in multi})[:1]
    return sorted(f for f in files if f.suffix.lower() in (".csv", ".gpkg", ".shp"))


def _process(dataset: str, zpath: Path, parquet_dir: Path, size: int | None, md5: str | None) -> int:
    """Extract -> convert -> verify -> delete extracted files and zip. One at a time."""
    from uprn_resolver.convert import convert  # imported lazily: owned by another module

    with _CONVERT_LOCK:
        out = zpath.parent
        extra = out / f".x_{zpath.stem}"  # private extraction dir, removed afterwards
        if extra.exists():
            shutil.rmtree(extra)
        extra.mkdir(parents=True)
        try:
            files = _unzip(zpath, extra)
            srcs = _sources(dataset, files)
            if not srcs and dataset == "openmap_local":
                print(f"  {zpath.name}: no Building layer (e.g. overview tile), nothing to convert")
                srcs = []
            elif not srcs:
                raise RuntimeError(f"{zpath.name}: no convertible files found")
            if dataset == "open_toid":
                _wait_for_file(parquet_dir / "lids" / "lids.parquet", zpath.name)
            rows = 0
            for src in srcs:
                n = convert(dataset, src, parquet_dir)
                if not isinstance(n, int) or n < 0:
                    raise RuntimeError(f"{src.name}: convert returned {n!r}")
                rows += n
            if rows <= 0 and dataset not in ("open_toid", "openmap_local"):
                raise RuntimeError(f"{zpath.name}: converted 0 rows")
        except BaseException:
            shutil.rmtree(extra, ignore_errors=True)
            raise
        shutil.rmtree(extra)
        zpath.unlink()
    _record(parquet_dir, dataset, zpath.name, size, md5, rows)
    print(f"  converted {zpath.name}: {rows} rows; source deleted")
    return rows


def _fetch_and_process(dataset: str, d: dict, out: Path, parquet_dir: Path) -> int | None:
    name, size, md5 = d["fileName"], d.get("size"), d.get("md5")
    key = None
    if dataset in ("open_toid", "openmap_local") and d.get("area"):
        key = f"{dataset}/{str(d['area']).lower()}.parquet"
    if _already_converted(parquet_dir, name, md5, key):
        print(f"  {name}: in manifest, skipping")
        return None
    for attempt in (1, 2, 3):
        try:
            z = _stream(d["url"], out / name, size, md5)
            break
        except (OSError, requests.RequestException) as e:
            if attempt == 3 or isinstance(e, DiskSpaceError):
                raise
            print(f"  retrying {name} after: {e}")
    return _process(dataset, z, parquet_dir, size, md5)


def _run_all(dataset: str, items: list[dict], out: Path, parquet_dir: Path) -> None:
    errors: list[str] = []
    with ThreadPoolExecutor(DOWNLOAD_WORKERS) as ex:
        futs = [(d["fileName"], ex.submit(_fetch_and_process, dataset, d, out, parquet_dir)) for d in items]
        for name, f in futs:
            try:
                f.result()
            except DiskSpaceError:
                for _, g in futs:
                    g.cancel()
                raise
            except Exception as e:  # keep going with other files
                print(f"  FAILED {name}: {e}")
                errors.append(f"{name}: {e}")
    if errors:
        raise RuntimeError(f"{dataset}: {len(errors)} file(s) failed: " + "; ".join(errors))


def _os_listing(product: str) -> list[dict]:
    r = requests.get(f"{OS_API}/{product}/downloads", timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def _select(dataset: str, listing: list[dict], areas: list[str]) -> list[dict]:
    product, fmt, name_re, tiled = OS_PRODUCTS[dataset]
    listing = [d for d in listing if d.get("format") == fmt]
    if tiled:
        wanted = {a.strip().upper() for a in areas}
        if "ALL" in wanted:
            tiles = [d for d in listing if d.get("area") != "GB"]
            listing = tiles or listing  # per-tile files (converted one by one); GB only if no tiles
            # TQ first (Islington pilot), then the rest
            listing.sort(key=lambda d: (str(d.get("area", "")).upper() != "TQ", d["fileName"]))
        else:
            listing = [d for d in listing if str(d.get("area", "")).upper() in wanted]
            missing = wanted - {str(d["area"]).upper() for d in listing}
            if missing:
                raise ValueError(f"{product}: no {fmt} download for areas {sorted(missing)}")
    else:
        listing = [d for d in listing if d.get("area") == "GB"]
    if name_re:
        listing = [d for d in listing if re.search(name_re, d["fileName"])]
    if not listing:
        raise RuntimeError(f"{product}: nothing matched in OS Downloads API")
    return listing


def _download_os(dataset: str, raw_dir: Path, parquet_dir: Path, areas: list[str]) -> None:
    listing = _select(dataset, _os_listing(OS_PRODUCTS[dataset][0]), areas)
    _run_all(dataset, listing, raw_dir / dataset, parquet_dir)


def _latest_onsud_item() -> dict:
    """Newest ONSUD data zip on the ONS Open Geography Portal (not the user guide)."""
    r = requests.get(
        f"{ARCGIS}/search",
        params={
            "q": 'title:"ONS UPRN Directory" owner:ONSGeography_data type:"CSV Collection"',
            "sortField": "created",
            "sortOrder": "desc",
            "num": 30,
            "f": "json",
        },
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    for it in r.json()["results"]:
        if "user guide" not in it["title"].lower() and it["title"].startswith("ONS UPRN Directory ("):
            return it
    raise RuntimeError("no ONSUD release found on the ONS Open Geography Portal")


def _download_onsud(raw_dir: Path, parquet_dir: Path) -> None:
    item = _latest_onsud_item()
    print(f"  ONSUD release: {item['title']} (item {item['id']})")
    d = {
        "fileName": item["name"],
        "size": item.get("size") if (item.get("size") or 0) > 0 else None,
        "md5": None,
        "url": f"{ARCGIS}/content/items/{item['id']}/data",
    }
    _run_all("onsud", [d], raw_dir / "onsud", parquet_dir)


def download(datasets: list[str] | None, raw_dir: Path, areas: list[str] | None = None) -> None:
    """Download, convert and delete the given datasets (default: all).

    ``raw_dir`` is ``data/raw``; Parquet goes to the sibling ``data/parquet``.
    ``areas`` selects 100 km tiles for tiled products (open_toid, openmap_local).
    Default: env UPRN_RESOLVER_AREAS (comma-separated, e.g. "TQ"; "all" or unset = all tiles).
    """
    raw_dir = Path(raw_dir)
    parquet_dir = raw_dir.parent / "parquet"
    if areas is None:
        env = os.environ.get("UPRN_RESOLVER_AREAS", "")
        areas = [a.strip() for a in env.split(",") if a.strip()] or DEFAULT_AREAS
    datasets = datasets or ALL_DATASETS
    unknown = set(datasets) - set(ALL_DATASETS)
    if unknown:
        raise ValueError(f"unknown datasets {sorted(unknown)}; choose from {ALL_DATASETS}")
    for ds in datasets:
        print(f"[download] {ds}")
        if ds == "onsud":
            _download_onsud(raw_dir, parquet_dir)
        else:
            _download_os(ds, raw_dir, parquet_dir, areas)
