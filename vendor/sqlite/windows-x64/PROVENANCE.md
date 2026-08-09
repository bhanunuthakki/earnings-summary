# SQLite 3.53.4 Windows x64 runtime

- Publisher: SQLite project
- Version: 3.53.4, released 2026-07-24
- Official archive: https://www.sqlite.org/2026/sqlite-dll-win-x64-3530400.zip
- Official archive size: 1,370,147 bytes
- Official archive SHA3-256: `deddee963c810d1eeac3ce5e15c7c41da21a1c54d7a39cf54fbf577d2f50de3a` <!-- pragma: allowlist secret -->
- Extracted `sqlite3.dll` size: 3,285,504 bytes
- Extracted `sqlite3.dll` SHA3-256: `844d00bdf5ba9a52d61cd3fd244a7efffdb89d7da119701fb47c172043c5c1d3` <!-- pragma: allowlist secret -->
- Extracted `sqlite3.dll` SHA-256: `ab57d0437795ecc757cb693f32ea224173fa9856594d95cfa6b5033e645cd1ec` <!-- pragma: allowlist secret -->
- Retrieved and verified: 2026-08-08
- WAL-reset fix basis: https://www.sqlite.org/wal.html#walresetbug
- Python DLL search API: https://docs.python.org/3/library/os.html#os.add_dll_directory

The archive's SHA3-256 was checked against the SQLite project's download page
before extraction. The extracted DLL's pinned SHA-256 is enforced once at
managed Python process startup by `execution/sqlite_bootstrap.py` and is
covered by a repository test. SHA-256 is used at runtime because this machine's
hardware-accelerated implementation materially reduces startup latency.

SQLite is in the public domain: https://www.sqlite.org/copyright.html
