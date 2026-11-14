import sys
import types
from pathlib import Path

import pytest

from uprn_resolver import cli


def _install(monkeypatch, calls, hmlr_fails=False):
    def mod(name, **fns):
        m = types.ModuleType(f"uprn_resolver.{name}")
        for k, v in fns.items():
            setattr(m, k, v)
        monkeypatch.setitem(sys.modules, f"uprn_resolver.{name}", m)
        monkeypatch.setattr(cli.__package__ and sys.modules["uprn_resolver"], name, m, raising=False)

    def hm_fail(*a):
        raise RuntimeError("boom")

    mod("download", download=lambda ds, raw: calls.append(("download", ds, raw)))
    mod("stage", stage=lambda lad, raw, st: calls.append(("stage", lad, raw, st)))
    mod(
        "build",
        build=lambda lad, st, out, include_postcode=False, **kw: (
            calls.append(("build", lad, st, out, include_postcode)) or {"uprns": 3}
        ),
        build_all=lambda st, out, skip_existing=True, **kw: (
            calls.append(("build_all", st, out, skip_existing, kw)) or {"built": {"A": {}}, "skipped": [], "failed": {}}
        ),
    )
    mod("qa", qa=lambda lad, out, report: calls.append(("qa", lad, out, report)) or {"report_path": str(report)})
    for n in ("streets", "epc", "inspire", "ros"):
        if hmlr_fails:
            mod(n, **{f"download_{n}": hm_fail, f"stage_{n}": hm_fail})
        else:
            mod(
                n,
                **{
                    f"download_{n}": lambda raw, n=n: calls.append((f"d_{n}",)),
                    f"stage_{n}": lambda lad, raw, st, n=n: calls.append((f"s_{n}",)),
                },
            )
    if hmlr_fails:
        mod("hmlr", download_hmlr=hm_fail, stage_hmlr=hm_fail)
    else:
        mod(
            "hmlr",
            download_hmlr=lambda raw: calls.append(("hd", raw)),
            stage_hmlr=lambda lad, raw, st: calls.append(("hs", lad, raw, st)),
        )


def test_parse_defaults():
    a = cli.build_parser().parse_args(["build", "--lad", "E09000019"])
    assert a.data_dir == Path("data") and a.lad == "E09000019" and not a.include_postcode


def test_parse_requires_lad():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["stage"])


def test_download_args():
    a = cli.build_parser().parse_args(["download", "--datasets", "onsud", "lids", "--areas", "TQ"])
    assert a.datasets == ["onsud", "lids"] and a.areas == ["TQ"]


def test_stage_calls_hmlr(monkeypatch):
    calls = []
    _install(monkeypatch, calls)
    cli.main(["--data-dir", "d", "stage", "--lad", "X"])
    assert calls == [
        ("stage", "X", Path("d/raw"), Path("d/staged")),
        ("hs", "X", Path("d/raw"), Path("d/staged")),
        ("s_streets",),
        ("s_epc",),
        ("s_inspire",),
        ("s_ros",),
    ]


def test_hmlr_failure_warns(monkeypatch, capsys):
    calls = []
    _install(monkeypatch, calls, hmlr_fails=True)
    assert cli.main(["download"]) == 0
    cli.main(["stage", "--lad", "X"])
    assert capsys.readouterr().err.count("warning") == 10


def test_build_and_stats(monkeypatch, capsys):
    calls = []
    _install(monkeypatch, calls)
    cli.main(["build", "--lad", "X", "--include-postcode"])
    assert calls[0] == ("build", "X", Path("data/staged"), Path("data/out"), True)
    assert "uprns: 3" in capsys.readouterr().out


def test_all_runs_everything(monkeypatch):
    calls = []
    _install(monkeypatch, calls)
    cli.main(["all", "--lad", "X"])
    assert [c[0] for c in calls] == [
        "download",
        "hd",
        "d_streets",
        "d_epc",
        "d_inspire",
        "d_ros",
        "stage",
        "hs",
        "s_streets",
        "s_epc",
        "s_inspire",
        "s_ros",
        "build",
        "qa",
    ]


def test_build_all(monkeypatch):
    calls = []
    _install(monkeypatch, calls)
    assert cli.main(["build", "--all", "--rebuild", "--threads", "2"]) == 0
    assert calls == [
        ("build_all", Path("data/staged"), Path("data/out"), False, {"include_postcode": False, "threads": 2})
    ]


def test_build_all_lad_exclusive():
    for argv in (["build", "--all", "--lad", "X"], ["build"]):
        with pytest.raises(SystemExit):
            cli.build_parser().parse_args(argv)


def _fake(monkeypatch, name, calls, **fns):
    """Install a fake uprn_resolver.<name> whose functions record (name.fn, args, kwargs)."""
    m = types.ModuleType(f"uprn_resolver.{name}")
    for fn, ret in fns.items():
        setattr(m, fn, lambda *a, _fn=fn, _ret=ret, **kw: calls.append((f"{name}.{_fn}", a, kw)) or _ret)
    monkeypatch.setitem(sys.modules, f"uprn_resolver.{name}", m)
    monkeypatch.setattr(sys.modules["uprn_resolver"], name, m, raising=False)


ALL = {"built": ["A"], "done": ["A"], "skipped": [], "failed": {}}


@pytest.mark.parametrize(
    "argv,mod,one,many",
    [
        (["address"], "address", "address_local", "address_local_all"),
        (["provenance"], "provenance", "provenance_for_lad", "provenance_all"),
        (["poi"], "poi", "poi_for_lad", "poi_all"),
        (["parcels"], "parcels", "parcels_for_lad", "parcels_all"),
        (["osm"], "osm", "osm_for_lad", "osm_all"),
    ],
)
def test_per_lad_commands(monkeypatch, argv, mod, one, many):
    calls = []
    _fake(monkeypatch, mod, calls, **{one: {"n": 1}, many: ALL})
    assert cli.main([*argv, "--lad", "X"]) == 0
    assert calls == [(f"{mod}.{one}", ("X", Path("data/staged"), Path("data/out"), Path("data/parquet")), {})]
    calls.clear()
    assert cli.main([*argv, "--all", "--rebuild"]) == 0
    name, a, kw = calls[0]
    assert name == f"{mod}.{many}" and kw["skip_existing"] is False
    calls.clear()
    cli.main([*argv, "--all"])
    assert calls[0][2]["skip_existing"] is True


def test_all_failed_exit_code(monkeypatch):
    calls = []
    _fake(monkeypatch, "address", calls, address_local_all={"built": [], "skipped": [], "failed": ["X"]})
    assert cli.main(["address", "--all"]) == 1


def test_poi_download_assign(monkeypatch):
    calls = []
    _fake(monkeypatch, "poi", calls, download_places=3, assign_places_lad={"n": 1})
    assert cli.main(["poi", "--download", "--assign"]) == 0
    assert [c[0] for c in calls] == ["poi.download_places", "poi.assign_places_lad"]
    with pytest.raises(SystemExit):
        cli.main(["poi"])


def test_outlines_both(monkeypatch):
    calls = []
    _fake(monkeypatch, "osm_buildings", calls, osm_outline_all=ALL)
    _fake(monkeypatch, "msbuildings", calls, ms_outline_all=ALL)
    assert cli.main(["outlines", "--all"]) == 0
    assert [c[0] for c in calls] == ["osm_buildings.osm_outline_all", "msbuildings.ms_outline_all"]
    calls.clear()
    cli.main(["outlines", "--source", "ms", "--all"])
    assert [c[0] for c in calls] == ["msbuildings.ms_outline_all"]


def test_resolve_and_lookup(monkeypatch, capsys):
    calls = []
    _fake(
        monkeypatch,
        "resolve",
        calls,
        resolve_lad={"q": 1},
        resolve_all=ALL,
        lookup={"uprn": 5, "street_name": "High St"},
    )
    assert cli.main(["resolve", "--lad", "X"]) == 0
    assert cli.main(["resolve", "--all", "--rebuild"]) == 0
    assert calls[1][2]["skip_existing"] is False
    calls.clear()
    assert cli.main(["lookup", "5"]) == 0
    assert calls == [("resolve.lookup", (5, Path("data/out")), {"local": False})]
    assert '"street_name": "High St"' in capsys.readouterr().out
    cli.main(["lookup", "5", "--local"])
    assert calls[-1][2] == {"local": True}


def test_lookup_missing(monkeypatch):
    _fake(monkeypatch, "resolve", [], lookup=None)
    assert cli.main(["lookup", "5"]) == 1


def test_publish_dry_run_default(monkeypatch):
    calls = []
    _fake(monkeypatch, "publish", calls, publish=[])
    cli.main(["publish", "--release", "2026-10", "--dest", "s3://a/b/"])
    assert calls[0][2]["dry_run"] is True and calls[0][1][:3] == (Path("data/out"), "2026-10", "s3://a/b/")
    cli.main(["publish", "--release", "r", "--dest", "s3://a/b/", "--execute"])
    assert calls[1][2]["dry_run"] is False
    assert calls[0][2]["lean"] is True
    cli.main(["publish", "--release", "r", "--dest", "s3://a/b/", "--full"])
    assert calls[2][2]["lean"] is False
    assert calls[0][2]["single_file"] is True
    cli.main(["publish", "--release", "r", "--dest", "s3://a/b/", "--per-lad"])
    assert calls[3][2]["single_file"] is False


def test_national_validate(monkeypatch, capsys):
    calls = []
    _fake(monkeypatch, "national", calls, national={"lads": 2})
    _fake(monkeypatch, "validate", calls, validate={"status": "FAIL", "summary": {}})
    assert cli.main(["national"]) == 0
    assert calls[0][1] == (Path("data/out"), Path("data/out/national/national.html"))
    assert cli.main(["validate", "--lad", "X"]) == 1
    assert calls[1][1][4] == ["X"]


def test_ni(monkeypatch, tmp_path):
    calls = []
    _fake(monkeypatch, "ni", calls, extract_ni={}, ni_address_points={})
    pbf = tmp_path / "united-kingdom-261020.osm.pbf"
    pbf.write_bytes(b"")
    cli.main(["--data-dir", str(tmp_path), "ni", "extract"])
    cli.main(["--data-dir", str(tmp_path), "ni", "addresses"])
    assert calls[0] == ("ni.extract_ni", (pbf, tmp_path / "parquet"), {})
    assert calls[1][0] == "ni.ni_address_points"
    with pytest.raises(SystemExit):
        cli.main(["--data-dir", str(tmp_path / "empty"), "ni", "extract"])


def test_download_extras(monkeypatch, tmp_path):
    calls = []
    pbf = tmp_path / "x.osm.pbf"
    _fake(monkeypatch, "bng", calls, fetch_grid=None)
    _fake(monkeypatch, "osm", calls, convert_osm={})
    _fake(monkeypatch, "osm_buildings", calls, convert_osm_buildings={})
    _fake(monkeypatch, "poi", calls, download_places=1)
    _fake(monkeypatch, "provenance", calls, download_provenance_sources=None)
    cli.main(
        [
            "--data-dir",
            str(tmp_path),
            "download",
            "--sources",
            "ostn15",
            "provenance",
            "osm",
            "osm_buildings",
            "overture",
            "--pbf",
            str(pbf),
        ]
    )
    assert [c[0] for c in calls] == [
        "bng.fetch_grid",
        "provenance.download_provenance_sources",
        "osm.convert_osm",
        "osm_buildings.convert_osm_buildings",
        "poi.download_places",
    ]


def test_pipeline_order(monkeypatch):
    calls = []
    _install(monkeypatch, calls)
    for mod, fns in (
        ("address", {"address_local_all": ALL}),
        ("provenance", {"provenance_all": ALL}),
        ("poi", {"poi_all": ALL}),
        ("osm", {"osm_all": ALL}),
        ("ppd", {"ppd_all": ALL}),
        ("osm_buildings", {"osm_outline_all": ALL}),
        ("msbuildings", {"ms_outline_all": ALL}),
        ("parcels", {"parcels_all": ALL}),
        ("resolve", {"resolve_all": ALL}),
        ("national", {"national": {}}),
        ("validate", {"validate": {"status": "PASS", "summary": {}}}),
    ):
        _fake(monkeypatch, mod, calls, **fns)
    stage_all = {"staged": ["A"], "skipped": [], "failed": {}, "ignored": {}}
    _fake(monkeypatch, "stage", calls, stage_all=stage_all)
    assert cli.main(["pipeline", "--all"]) == 0
    assert [c[0] for c in calls] == [
        "stage.stage_all",
        "build_all",
        "provenance.provenance_all",
        "poi.poi_all",
        "osm.osm_all",
        "ppd.ppd_all",
        "address.address_local_all",
        "osm_buildings.osm_outline_all",
        "msbuildings.ms_outline_all",
        "parcels.parcels_all",
        "resolve.resolve_all",
        "national.national",
        "validate.validate",
    ]
    calls.clear()
    assert cli.main(["pipeline", "--all", "--skip-extras"]) == 0
    assert [c[0] for c in calls][:2] == ["stage.stage_all", "build_all"] and "address.address_local_all" not in [
        c[0] for c in calls
    ]


def test_pipeline_extras_warn_and_continue(monkeypatch, capsys):
    calls = []
    _install(monkeypatch, calls)
    _fake(monkeypatch, "stage", calls, stage_all={"staged": [], "skipped": [], "failed": {}, "ignored": {}})
    _fake(monkeypatch, "resolve", calls, resolve_all=ALL)
    _fake(monkeypatch, "validate", calls, validate={"status": "WARN", "summary": {}})
    for m in ("address", "provenance", "poi", "osm", "ppd", "osm_buildings", "msbuildings", "parcels", "national"):
        monkeypatch.setitem(sys.modules, f"uprn_resolver.{m}", None)  # import fails
    assert cli.main(["pipeline", "--all"]) == 0
    err = capsys.readouterr().err
    assert "warning: address failed" in err and "national roll-up failed" in err
    assert any(c[0] == "resolve.resolve_all" for c in calls)


def test_download_sources_parse():
    a = cli.build_parser().parse_args(["download", "--sources", "osm", "ostn15"])
    assert a.sources == ["osm", "ostn15"]
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["download", "--sources", "nope"])


def _refresh_calls(monkeypatch, tmp_path, fail=None):
    calls = []
    for name in (
        "_download",
        "_hmlr_archive",
        "_stage",
        "_build",
        "_provenance",
        "_poi",
        "_osm",
        "_ppd",
        "_address",
        "_outlines",
        "_parcels",
        "_resolve",
        "_national",
        "_validate",
        "_publish",
    ):

        def f(args, name=name):
            calls.append(name)
            if name == fail:
                raise RuntimeError("boom")
            return 0

        monkeypatch.setattr(cli, name, f)
    monkeypatch.setitem(cli._COMMANDS, "refresh", cli._refresh)
    return calls


def test_refresh_order_and_clean_state(monkeypatch, tmp_path, capsys):
    calls = _refresh_calls(monkeypatch, tmp_path)
    assert cli.main(["--data-dir", str(tmp_path), "refresh", "--release", "2026-11"]) == 0
    assert calls == [
        "_download",
        "_hmlr_archive",
        "_stage",
        "_build",
        "_provenance",
        "_poi",
        "_osm",
        "_ppd",
        "_address",
        "_outlines",
        "_parcels",
        "_resolve",
        "_national",
        "_validate",
        "_publish",
    ]
    assert "Refresh summary" in capsys.readouterr().out
    assert not (tmp_path / "refresh.state.json").exists()


def test_refresh_failure_resumes(monkeypatch, tmp_path, capsys):
    calls = _refresh_calls(monkeypatch, tmp_path, fail="_build")
    argv = ["--data-dir", str(tmp_path), "refresh", "--release", "2026-11", "--skip-extras"]
    assert cli.main(argv) == 1
    assert "build: FAILED" in capsys.readouterr().out
    assert (tmp_path / "refresh.state.json").exists()
    calls.clear()
    _refresh_calls_ok = _refresh_calls(monkeypatch, tmp_path)
    assert cli.main(argv) == 0
    assert "_download" not in _refresh_calls_ok and "_stage" not in _refresh_calls_ok
    assert _refresh_calls_ok[0] == "_build"


def test_hmlr_archive_command(monkeypatch, tmp_path, capsys):
    m = types.ModuleType("uprn_resolver.hmlr")
    m.archive_hmlr = lambda raw: {"new": ["a.parquet"], "archived": ["a.parquet"], "missing": []}
    monkeypatch.setitem(sys.modules, "uprn_resolver.hmlr", m)
    monkeypatch.setattr(sys.modules["uprn_resolver"], "hmlr", m, raising=False)
    assert cli.main(["--data-dir", str(tmp_path), "hmlr-archive"]) == 0
    assert "1 new" in capsys.readouterr().out


def test_ppd_command(monkeypatch):
    calls = []
    _fake(monkeypatch, "ppd", calls, download_ppd={"rows": 1}, ppd_lad={"n": 1}, ppd_all=ALL)
    assert cli.main(["ppd", "--download", "--all"]) == 0
    assert [c[0] for c in calls] == ["ppd.download_ppd", "ppd.ppd_all"]
    assert calls[0][1] == (Path("data/parquet"),) and calls[1][2]["skip_existing"] is True
    calls.clear()
    assert cli.main(["ppd", "--lad", "E1", "--rebuild"]) == 0
    assert calls == [("ppd.ppd_lad", ("E1", Path("data/staged"), Path("data/out"), Path("data/parquet")), {})]
    calls.clear()
    assert cli.main(["ppd", "--download"]) == 0
    assert [c[0] for c in calls] == ["ppd.download_ppd"]
    with pytest.raises(SystemExit):
        cli.main(["ppd"])


def test_publish_extra_flags(monkeypatch):
    calls = []
    _fake(monkeypatch, "publish", calls, publish=[])
    cli.main(["publish", "--release", "r", "--dest", "s3://a/b/"])
    kw = calls[0][2]
    assert kw["force"] is False and kw["stac"] is True and kw["national_files"] is False
    assert kw["changelog_against"] is None and kw["source_manifest"] is None
    cli.main(
        ["publish", "--release", "r", "--dest", "s3://a/b/", "--force", "--no-stac", "--national-files",
         "--changelog-against", "old", "--source-manifest", "m.json"]
    )  # fmt: skip
    kw = calls[1][2]
    assert kw["force"] is True and kw["stac"] is False and kw["national_files"] is True
    assert kw["changelog_against"] == Path("old") and kw["source_manifest"] == Path("m.json")


def test_release_extras_command(monkeypatch, tmp_path):
    calls = []
    root = tmp_path / "rel" / "r"
    (root / "uprn").mkdir(parents=True)
    _fake(monkeypatch, "publish", calls, write_national=[], diff_releases={}, write_manifest={}, write_latest={})
    _fake(monkeypatch, "stac", calls, build_stac={})
    _fake(monkeypatch, "tiles", calls, build={"features": 1})
    sys.modules["uprn_resolver.tiles"].KINDS = {"uprn": 1}
    sys.modules["uprn_resolver.tiles"].RELEASE_KINDS = ("uprn",)
    argv = ["release-extras", "--release", "r", "--stage-dir", str(tmp_path / "rel")]
    assert cli.main(argv) == 0
    assert [c[0] for c in calls] == ["stac.build_stac", "publish.write_manifest", "publish.write_latest"]
    calls.clear()
    assert cli.main([*argv, "--no-stac", "--national-files", "--changelog-against", "old", "--tiles"]) == 0
    assert [c[0] for c in calls] == [
        "publish.write_national", "publish.diff_releases", "tiles.build", "publish.write_manifest",
        "publish.write_latest",
    ]  # fmt: skip
    with pytest.raises(SystemExit):
        cli.main(["release-extras", "--release", "missing", "--stage-dir", str(tmp_path)])


def test_release_extras_stac_failure(monkeypatch, tmp_path):
    (tmp_path / "r" / "uprn").mkdir(parents=True)
    _fake(monkeypatch, "publish", [], write_manifest={}, write_latest={})
    _fake(monkeypatch, "stac", [], build_stac={"missing_assets": ["x"]})
    assert cli.main(["release-extras", "--release", "r", "--stage-dir", str(tmp_path)]) == 1


def test_changelog_and_stac_validate(monkeypatch, capsys):
    calls = []
    _fake(monkeypatch, "publish", calls, diff_releases={"tables": {"uprn": {"added": 2, "removed": 1}}})
    assert cli.main(["changelog", "--old", "a", "--new", "b"]) == 0
    assert calls[0][1] == (Path("a"), Path("b")) and calls[0][2] == {"write": True}
    assert "+2 -1" in capsys.readouterr().out
    _fake(monkeypatch, "stac", calls, validate_stac={"missing_assets": []})
    assert cli.main(["stac-validate", "--release", "r"]) == 0
    assert calls[-1][1] == (Path("data/release/r/stac"),)
    _fake(monkeypatch, "stac", calls, validate_stac={"invalid": "bad"})
    assert cli.main(["stac-validate", "--stac-dir", "s"]) == 1
    with pytest.raises(SystemExit):
        cli.main(["stac-validate"])


def test_tiles_command(monkeypatch):
    calls = []
    _fake(monkeypatch, "tiles", calls, build={"features": 1})
    sys.modules["uprn_resolver.tiles"].KINDS = {"uprn": 1, "buildings": 2}
    assert cli.main(["tiles", "--release", "r", "--lad-prefix", "E09"]) == 0
    assert [c[1][1] for c in calls] == ["uprn", "buildings"]
    assert calls[0][1][0] == Path("data/release/r") and calls[0][2]["prefix"] == "E09"
    calls.clear()
    cli.main(["tiles", "--release", "r", "--kind", "buildings"])
    assert [c[1][1] for c in calls] == ["buildings"]
    with pytest.raises(SystemExit):
        cli.main(["tiles", "--release", "r", "--kind", "nope"])


def test_anomalies_command(monkeypatch):
    calls = []
    _fake(monkeypatch, "anomalies", calls, main=None)
    assert cli.main(["anomalies", "--only", "a", "b", "--doc", "d.md"]) == 0
    argv = calls[0][1][0]
    assert argv[:2] == ["--parquet-dir", "data/parquet"] and argv[-5:] == ["--only", "a", "b", "--doc", "d.md"]
