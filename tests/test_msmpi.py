import hashlib

import pytest

from wheelbuild import msmpi


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_msi(directory, files):
    """An unpacked MSI: its cabinet's files under their File-table keys."""
    directory.mkdir(parents=True)
    for name, data in files.items():
        (directory / name).write_bytes(data)
    return directory


# The two cabinets of msmpisetup.exe as 7z unpacks them, with stand-in bytes:
# the x86 one names its DLL msmpi.dll, the x64 one keys its own msmpi64.dll.
X86_FILES = {
    "msmpi.dll": b"x86 msmpi",
    "msmpires.dll": b"x86 msmpires",
    "mpiexec.exe": b"x86 mpiexec",
    "smpd.exe": b"x86 smpd",
}
X64_FILES = {
    **X86_FILES,
    "msmpi64.dll": b"x64 msmpi",
    "msmpires64.dll": b"x64 msmpires",
    "mpiexec.exe": b"x64 mpiexec",
    "smpd.exe": b"x64 smpd",
}

# The recorded table, re-pinned to the stand-in x64 bytes.
STAND_IN_FILES = tuple(
    msmpi.RedistributableFile(
        name=entry.name, msi_key=entry.msi_key, sha256=_digest(X64_FILES[entry.msi_key])
    )
    for entry in msmpi.REDISTRIBUTABLE_FILES
)


def test_the_recorded_dll_is_the_x64_one_the_decision_names():
    (dll,) = [e for e in msmpi.REDISTRIBUTABLE_FILES if e.name == "msmpi.dll"]

    assert dll.msi_key == "msmpi64.dll"
    assert dll.sha256 == (
        "d8f74689e83d7e5a7277de15b221edea91f529529f8a16fd0e510236c0705dcb"
    )


def test_the_wheel_takes_the_dll_and_both_launcher_binaries_and_no_resources():
    names = {entry.name for entry in msmpi.REDISTRIBUTABLE_FILES}

    assert names == {"msmpi.dll", "mpiexec.exe", "smpd.exe"}


def test_every_recorded_digest_is_a_sha256():
    digests = [entry.sha256 for entry in msmpi.REDISTRIBUTABLE_FILES]
    digests += [msmpi.INSTALLER_SHA256, *msmpi.MICROSOFT_TEXTS.values()]

    for digest in digests:
        assert len(digest) == 64
        assert int(digest, 16) >= 0
    assert len(set(digests)) == len(digests)


def test_the_x64_msi_is_picked_whichever_order_7z_carves_them(tmp_path):
    x86 = _write_msi(tmp_path / "2", X86_FILES)
    x64 = _write_msi(tmp_path / "4", X64_FILES)

    assert msmpi.x64_tree([x86, x64]) == x64
    assert msmpi.x64_tree([x64, x86]) == x64


def test_an_installer_with_only_the_x86_msi_is_refused(tmp_path):
    """A plain `7z x` sees the first cabinet only, which is the x86 one."""
    x86 = _write_msi(tmp_path / "2", X86_FILES)

    with pytest.raises(msmpi.MissingX64InstallerError, match=r"msmpi64\.dll"):
        msmpi.x64_tree([x86])


def test_two_msis_claiming_the_x64_runtime_are_refused(tmp_path):
    trees = [_write_msi(tmp_path / name, X64_FILES) for name in ("2", "4")]

    with pytest.raises(msmpi.MissingX64InstallerError, match="found 2"):
        msmpi.x64_tree(trees)


def test_the_x64_files_are_written_under_their_installed_names(tmp_path):
    x64 = _write_msi(tmp_path / "4", X64_FILES)
    out = tmp_path / "msmpi"

    written = msmpi.take(x64, out, files=STAND_IN_FILES)

    assert sorted(path.name for path in written) == [
        "mpiexec.exe",
        "msmpi.dll",
        "smpd.exe",
    ]
    assert (out / "msmpi.dll").read_bytes() == b"x64 msmpi"
    assert not (out / "msmpires.dll").exists()


def test_taking_the_x86_msi_fails_rather_than_shipping_it(tmp_path):
    x86 = _write_msi(tmp_path / "2", X86_FILES)
    out = tmp_path / "msmpi"

    with pytest.raises(FileNotFoundError, match=r"msmpi\.dll: no msmpi64\.dll"):
        msmpi.take(x86, out, files=STAND_IN_FILES)
    assert not out.exists()


def test_the_x86_dll_under_the_x64_key_fails_the_checksum(tmp_path):
    """The x86 cabinet's files hash differently, so even a tree that has been
    given the x64 key names cannot pass for the x64 MSI.
    """
    disguised = {**X86_FILES, "msmpi64.dll": X86_FILES["msmpi.dll"]}
    tree = _write_msi(tmp_path / "2", disguised)

    with pytest.raises(msmpi.ChecksumMismatchError, match=r"^msmpi\.dll"):
        msmpi.take(tree, tmp_path / "msmpi", files=STAND_IN_FILES)


def test_a_mismatch_names_the_file_and_writes_nothing(tmp_path):
    tampered = {**X64_FILES, "smpd.exe": b"not Microsoft's smpd"}
    tree = _write_msi(tmp_path / "4", tampered)
    out = tmp_path / "msmpi"

    with pytest.raises(msmpi.ChecksumMismatchError, match=r"^smpd\.exe: SHA-256"):
        msmpi.take(tree, out, files=STAND_IN_FILES)
    assert not out.exists()


def test_the_real_table_rejects_stand_in_bytes(tmp_path):
    """The recorded digests are what is checked by default."""
    tree = _write_msi(tmp_path / "4", X64_FILES)

    with pytest.raises(
        msmpi.ChecksumMismatchError, match=r"msmpi\.dll \(MSI key msmpi64\.dll\)"
    ):
        msmpi.take(tree, tmp_path / "msmpi")


def test_microsoft_texts_are_checked_against_their_digests(tmp_path):
    texts = {"MicrosoftMPI_Redistributable_EULA.rtf": b"eula"}
    tree = _write_msi(tmp_path / "4", texts)

    msmpi.verify_texts(tree, {name: _digest(data) for name, data in texts.items()})
    with pytest.raises(msmpi.ChecksumMismatchError, match="EULA"):
        msmpi.verify_texts(tree)


def test_a_verified_installer_already_there_is_not_downloaded_again(
    tmp_path, monkeypatch
):
    installer = tmp_path / "msmpisetup.exe"
    installer.write_bytes(b"installer")

    def no_network(url, timeout):
        raise AssertionError(f"downloaded {url} again within {timeout} s")

    monkeypatch.setattr(msmpi.urllib.request, "urlopen", no_network)

    assert msmpi.fetch(installer, expected=_digest(b"installer")) == installer


class _Response:
    def __init__(self, data):
        self.data = data

    def read(self):
        return self.data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _serving(data):
    """An ``urlopen`` that answers the pinned URL with ``data``."""

    def urlopen(url, timeout):
        assert url == msmpi.INSTALLER_URL
        assert timeout > 0
        return _Response(data)

    return urlopen


def test_a_download_with_the_wrong_bytes_is_refused_and_removed(tmp_path, monkeypatch):
    installer = tmp_path / "msmpisetup.exe"
    monkeypatch.setattr(msmpi.urllib.request, "urlopen", _serving(b"tampered"))

    with pytest.raises(msmpi.ChecksumMismatchError, match=r"^msmpisetup\.exe"):
        msmpi.fetch(installer)
    assert list(tmp_path.iterdir()) == []


def test_a_download_with_the_pinned_bytes_is_kept(tmp_path, monkeypatch):
    installer = tmp_path / "msmpisetup.exe"
    monkeypatch.setattr(msmpi.urllib.request, "urlopen", _serving(b"installer"))

    msmpi.fetch(installer, expected=_digest(b"installer"))

    assert installer.read_bytes() == b"installer"
    assert list(tmp_path.iterdir()) == [installer]


def test_unpack_carves_the_msis_then_unpacks_each(tmp_path, monkeypatch):
    commands = []

    def fake_7z(command):
        commands.append(command)
        if "-t#" in command:
            parts = tmp_path / "work" / "parts"
            parts.mkdir(parents=True)
            for name in ("1", "2.msi", "3", "4.msi"):
                (parts / name).write_bytes(b"")

    monkeypatch.setattr(msmpi, "check_call", fake_7z)

    trees = msmpi.unpack(tmp_path / "msmpisetup.exe", tmp_path / "work")

    assert trees == [tmp_path / "work" / "2", tmp_path / "work" / "4"]
    assert commands[0][:3] == ["7z", "x", "-t#"]
    assert [command[2] for command in commands[1:]] == [
        str(tmp_path / "work" / "parts" / "2.msi"),
        str(tmp_path / "work" / "parts" / "4.msi"),
    ]
