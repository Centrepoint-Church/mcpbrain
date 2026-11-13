"""The daemon writes the fleet health beacon itself, hourly.

It used to depend on a separate launchd/schtasks job that setup installed only
when fleet.folder_id was set explicitly, which no install does, so the fleet
folder held one beacon from 2026-06-18 and nobody could see fleet versions.
"""
from mcpbrain import daemon as d
from mcpbrain import fleet


def _dm(services):
    dm = d.Daemon.__new__(d.Daemon)
    dm._fleet_beacon_interval_s = 3600.0
    dm._last_fleet_beacon = None
    dm._clock = lambda: 500.0
    dm.ensure_services = lambda: services
    dm._google_services_pending = lambda: False
    return dm


def test_registered_and_on_by_default():
    cp = next(p for p in d._CADENCE_PASSES if p.name == "fleet_beacon")
    assert (cp.interval_attr, cp.last_attr, cp.fn_name) == (
        "_fleet_beacon_interval_s", "_last_fleet_beacon", "_run_fleet_beacon")
    assert not cp.needs_bulk_lock


def test_writes_beacon_with_the_drive_service(monkeypatch):
    seen = {}
    monkeypatch.setattr(fleet, "write_beacon", lambda home, svc: seen.update(svc=svc))
    dm = _dm({"drive_service": "DRIVE"})
    assert dm._run_fleet_beacon() == {"fleet_beacon": "written"}
    assert seen["svc"] == "DRIVE" and dm._last_fleet_beacon == 500.0


def test_not_due_does_nothing(monkeypatch):
    monkeypatch.setattr(fleet, "write_beacon", lambda *a: (_ for _ in ()).throw(AssertionError))
    dm = _dm({"drive_service": "DRIVE"})
    dm._last_fleet_beacon = 400.0
    assert dm._run_fleet_beacon() is None


def test_no_drive_yet_stays_due_while_services_retry(monkeypatch):
    monkeypatch.setattr(fleet, "write_beacon", lambda *a: (_ for _ in ()).throw(AssertionError))
    dm = _dm({})
    dm._google_services_pending = lambda: True
    assert dm._run_fleet_beacon() == {"fleet_beacon": "no_drive"}
    assert dm._last_fleet_beacon is None
