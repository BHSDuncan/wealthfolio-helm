#!/usr/bin/env python3
"""Verify a downloaded chart against its exact checksum before public upload."""
import hashlib
import hmac
from pathlib import Path
import re
import stat
import sys

NAME = re.compile(r'wealthfolio-[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?\.tgz')


def verify(archive, checksum):
    archive, checksum = Path(archive), Path(checksum)
    if not NAME.fullmatch(archive.name):
        raise ValueError('Unexpected chart archive basename')
    if checksum.name != archive.name + '.sha256' or archive.parent.resolve() != checksum.parent.resolve():
        raise ValueError('Checksum must accompany the exact chart basename')
    for path in (archive, checksum):
        if not stat.S_ISREG(path.lstat().st_mode):
            raise ValueError('Only regular artifact files are accepted')
    lines = checksum.read_text(encoding='ascii').splitlines()
    if len(lines) != 1:
        raise ValueError('Checksum must contain exactly one record')
    match = re.fullmatch(r'([a-f0-9]{64})  (wealthfolio-[^/\\\s]+\.tgz)', lines[0])
    if not match or match.group(2) != archive.name:
        raise ValueError('Checksum record must name the exact archive basename')
    digest = hashlib.sha256()
    with archive.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    if not hmac.compare_digest(digest.hexdigest(), match.group(1)):
        raise ValueError('Downloaded chart checksum mismatch')
    return True


if __name__ == '__main__':
    if len(sys.argv) != 3:
        raise SystemExit('usage: verify_checksum.py ARCHIVE CHECKSUM')
    try:
        verify(*sys.argv[1:])
    except (ValueError, OSError, UnicodeError):
        raise SystemExit('Artifact checksum verification failed; publication blocked')
    print('Downloaded chart checksum verified.')
