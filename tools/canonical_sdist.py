"""Normalize sdist container metadata without changing any member payload.

Setuptools 77's sdist ignores SOURCE_DATE_EPOCH for gzip and tar metadata.
This release-packaging stage makes that metadata explicit and reproducible.
Run after building, before hashing, signing or publishing the resulting sdist.
"""

from __future__ import annotations

import argparse
import gzip
import io
import os
import tarfile
import tempfile
from pathlib import Path


def canonicalize(path: Path, epoch: int) -> None:
    with tarfile.open(path, "r:gz") as archive:
        members = archive.getmembers()
        names = [m.name for m in members]
        if len(names) != len(set(names)):
            raise ValueError("duplicate sdist members")
        payloads = {}
        for member in members:
            if not (member.isfile() or member.isdir()):
                raise ValueError(f"unsupported sdist member: {member.name}")
            if member.isfile():
                source = archive.extractfile(member)
                assert source is not None
                payloads[member.name] = source.read()
    fd, temporary = tempfile.mkstemp(prefix=".canonical-", dir=path.parent)
    try:
        with (
            os.fdopen(fd, "wb") as output,
            gzip.GzipFile(filename="", mode="wb", fileobj=output, mtime=epoch) as gz,
            tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar,
        ):
            for member in sorted(members, key=lambda m: m.name):
                member.uid = member.gid = 0
                member.uname = member.gname = ""
                member.mtime = epoch
                member.pax_headers = {}
                data = payloads.get(member.name)
                tar.addfile(member, io.BytesIO(data) if data is not None else None)
        # Verify payload preservation before replacing the built archive.
        with tarfile.open(temporary, "r:gz") as verified:
            actual = {}
            for member in verified.getmembers():
                if member.isfile():
                    source = verified.extractfile(member)
                    assert source is not None
                    actual[member.name] = source.read()
            if actual != payloads:
                raise ValueError("sdist payload changed during normalization")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archives", type=Path, nargs="+")
    args = parser.parse_args()
    timestamp = int(os.environ["SOURCE_DATE_EPOCH"])
    for archive_path in args.archives:
        canonicalize(archive_path, timestamp)
