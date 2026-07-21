import sys

from mcpbrain import vcruntime


def test_is_x64_pe_detects_machine(tmp_path):
    # Minimal PE: 'MZ' at 0, e_lfanew at 0x3C -> 'PE\0\0' + machine word 0x8664 (x64).
    p = tmp_path / "fake.dll"
    buf = bytearray(0x100)
    buf[0:2] = b"MZ"
    buf[0x3C:0x40] = (0x80).to_bytes(4, "little")     # e_lfanew
    buf[0x80:0x84] = b"PE\x00\x00"
    buf[0x84:0x86] = (0x8664).to_bytes(2, "little")   # IMAGE_FILE_MACHINE_AMD64
    p.write_bytes(bytes(buf))
    assert vcruntime.is_x64_pe(str(p)) is True
    buf[0x84:0x86] = (0xAA64).to_bytes(2, "little")   # ARM64
    p.write_bytes(bytes(buf))
    assert vcruntime.is_x64_pe(str(p)) is False


def test_ensure_copies_only_x64_from_search(tmp_path, monkeypatch):
    # Given a fake source with an x64 MSVCP140_1.dll, ensure copies it into app_dir/vcruntime.
    src = tmp_path / "sys"; src.mkdir()
    dll = src / "MSVCP140_1.dll"
    dll.write_bytes(b"MZ" + b"\x00"*0x3A + (0x80).to_bytes(4, "little") + b"\x00"*0x40 + b"PE\x00\x00" + (0x8664).to_bytes(2, "little") + b"\x00"*0x80)
    app = tmp_path / "app"; app.mkdir()
    monkeypatch.setattr(vcruntime, "_SEARCH_DIRS", lambda: [src])
    monkeypatch.setattr(vcruntime, "_REQUIRED", ("MSVCP140_1.dll",))
    copied = vcruntime.ensure_vcruntime_dlls(str(app))
    assert "MSVCP140_1.dll" in copied
    assert (app / "vcruntime" / "MSVCP140_1.dll").exists()


def test_add_search_dir_is_noop_off_windows(tmp_path, monkeypatch):
    # Off-Windows this must do nothing (no os.add_dll_directory call, no raise),
    # regardless of whether <app_dir>/vcruntime exists.
    monkeypatch.setattr(sys, "platform", "darwin")
    vcruntime.add_search_dir(str(tmp_path))  # dir absent — must not raise

    vcdir = tmp_path / "vcruntime"; vcdir.mkdir()
    vcruntime.add_search_dir(str(tmp_path))  # dir present — still a no-op, must not raise
