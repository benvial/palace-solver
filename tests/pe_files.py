"""Synthetic PE files for the tests that read Windows binaries on any host.

A PE import table is read in-process with pefile, so a test needs no Windows,
only a file pefile parses. The files are built here rather than committed: a
synthetic PE that pefile itself parses is as honest as a real one for the only
part the readers look at, the two import directories, and the builder shows
exactly which bytes that is.
"""

import struct

#: Where the one section of a synthetic PE starts, in the file and in memory.
_FILE_ALIGNMENT = 0x200
_SECTION_RVA = 0x1000


class _ImportSection:
    """The bytes of one ``.idata`` section, laid out as they are appended.

    Every structure that points at another does so by RVA, so a structure is
    placed first and its address read back from :meth:`add`.
    """

    def __init__(self):
        self.data = bytearray()

    def add(self, blob):
        """Append ``blob`` 8-byte aligned and return its RVA."""
        self.data.extend(b"\0" * (-len(self.data) % 8))
        rva = _SECTION_RVA + len(self.data)
        self.data.extend(blob)
        return rva

    def put(self, rva, blob):
        """Overwrite the bytes at ``rva``, to fill a table reserved earlier."""
        offset = rva - _SECTION_RVA
        self.data[offset : offset + len(blob)] = blob

    def thunks(self, dll):
        """Return the name, lookup table and address table for one DLL.

        pefile drops a descriptor whose tables are empty, as a loader would
        have nothing to bind, so every DLL imports one function.
        """
        hint_name = self.add(struct.pack("<H", 0) + b"Function\0")
        name = self.add(dll.encode() + b"\0")
        lookup = self.add(struct.pack("<QQ", hint_name, 0))
        address = self.add(struct.pack("<QQ", hint_name, 0))
        return name, lookup, address


def _portable_executable(imports=(), delayed=()):
    """Return a minimal x86-64 PE importing ``imports``, delay-loading ``delayed``.

    One section holding both directories, and nothing a loader would need to
    run it: no code, no entry point. The two directory entries are what any PE
    reader goes to, and they are filled as a linker fills them.
    """
    section = _ImportSection()
    import_table = section.add(b"\0" * 20 * (len(imports) + 1))
    delay_table = section.add(b"\0" * 32 * (len(delayed) + 1))
    for index, dll in enumerate(imports):
        name, lookup, address = section.thunks(dll)
        # IMAGE_IMPORT_DESCRIPTOR: OriginalFirstThunk, TimeDateStamp,
        # ForwarderChain, Name, FirstThunk.
        section.put(
            import_table + 20 * index,
            struct.pack("<IIIII", lookup, 0, 0, name, address),
        )
    for index, dll in enumerate(delayed):
        name, lookup, address = section.thunks(dll)
        handle = section.add(b"\0" * 8)
        # IMAGE_DELAYLOAD_DESCRIPTOR: Attributes (1: the fields are RVAs),
        # DllNameRVA, ModuleHandleRVA, ImportAddressTableRVA,
        # ImportNameTableRVA, BoundImportAddressTableRVA,
        # UnloadInformationTableRVA, TimeDateStamp.
        section.put(
            delay_table + 32 * index,
            struct.pack("<IIIIIIII", 1, name, handle, address, lookup, 0, 0, 0),
        )

    raw = bytes(section.data) + b"\0" * (-len(section.data) % _FILE_ALIGNMENT)
    directories = [(0, 0)] * 16
    directories[1] = (import_table, 20 * (len(imports) + 1))  # IMPORT
    directories[13] = (delay_table, 32 * (len(delayed) + 1))  # DELAY_IMPORT
    optional_header = struct.pack(
        "<HBBIIIIIQIIHHHHHHIIIIHHQQQQII",
        0x20B,  # PE32+
        *(0, 0, 0, len(raw), 0, 0, 0),
        0x140000000,  # ImageBase
        _SECTION_RVA,  # SectionAlignment
        _FILE_ALIGNMENT,
        *(6, 0, 0, 0, 6, 0, 0),
        _SECTION_RVA + len(raw),  # SizeOfImage
        _FILE_ALIGNMENT,  # SizeOfHeaders
        0,
        3,  # IMAGE_SUBSYSTEM_WINDOWS_CUI
        *(0, 0x100000, 0x1000, 0x100000, 0x1000, 0),
        len(directories),
    ) + b"".join(struct.pack("<II", *entry) for entry in directories)
    headers = (
        b"MZ".ljust(0x3C, b"\0")
        + struct.pack("<I", 0x40)  # e_lfanew
        + b"PE\0\0"
        # IMAGE_FILE_HEADER: AMD64, one section, an executable image.
        + struct.pack("<HHIIIHH", 0x8664, 1, 0, 0, 0, len(optional_header), 0x22)
        + optional_header
        + struct.pack(
            "<8sIIIIIIHHI",
            b".idata",
            len(section.data),
            _SECTION_RVA,
            len(raw),
            _FILE_ALIGNMENT,
            *(0, 0, 0, 0),
            0xC0000040,  # initialised data, readable, writable
        )
    )
    return headers.ljust(_FILE_ALIGNMENT, b"\0") + raw


def write_pe(path, imports=(), delayed=()):
    """Write a synthetic PE to ``path`` and return the path."""
    path.write_bytes(_portable_executable(imports, delayed))
    return path
