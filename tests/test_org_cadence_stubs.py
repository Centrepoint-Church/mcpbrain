from mcpbrain import daemon as d


def test_org_cadence_passes_registered():
    names = {cp.name for cp in d._CADENCE_PASSES}
    assert {"org_contrib_upload", "org_import", "org_curate"} <= names


def test_org_cadence_defaults_and_keys_present():
    for key in ("org_contrib_upload_interval_s", "org_import_interval_s",
                "org_curate_interval_s"):
        assert key in d._CADENCE_DEFAULTS
        assert key in d._CADENCE_KEYS


def test_run_methods_exist_and_are_noops():
    # The Daemon class must define the three stub methods.
    for name in ("_run_org_contrib_upload", "_run_org_import", "_run_org_curate"):
        assert hasattr(d.Daemon, name)


def test_cadences_from_config_includes_org_keys(tmp_path):
    cad = d._cadences_from_config(str(tmp_path))
    assert cad["org_contrib_upload_interval_s"] == 86400.0
    assert cad["org_import_interval_s"] == 86400.0
    assert cad["org_curate_interval_s"] == 86400.0
