#!/usr/bin/env python3
"""Install checksum-locked user-local tools; never alters system packages."""
import argparse
import hashlib
import io
import json
import os
import pathlib
import platform
import tarfile
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--platform', default=platform.system().lower() + '-' + {'aarch64': 'arm64', 'x86_64': 'amd64'}.get(platform.machine(), platform.machine()))
p.add_argument('--offline', action='store_true')
a = p.parse_args()
lock = json.loads((ROOT / 'tools.lock.json').read_text())
if a.platform not in lock['platforms']:
    p.error('Supported locked platforms: ' + ', '.join(lock['platforms']))
dest = ROOT / '.tools/bin'
cache = ROOT / '.tools/downloads'
dest.mkdir(parents=True, exist_ok=True)
cache.mkdir(parents=True, exist_ok=True)
for name, spec in lock['platforms'][a.platform].items():
    archive = cache / spec['sha256']
    if not archive.exists():
        if a.offline:
            raise SystemExit('Missing cached tool: ' + name)
        with urllib.request.urlopen(spec['url'], timeout=120) as response:
            data = response.read()
        if hashlib.sha256(data).hexdigest() != spec['sha256']:
            raise SystemExit('Checksum mismatch: ' + name)
        archive.write_bytes(data)
    data = archive.read_bytes()
    if hashlib.sha256(data).hexdigest() != spec['sha256']:
        raise SystemExit('Cached checksum mismatch: ' + name)
    if spec['member']:
        with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as tar:
            data = tar.extractfile(spec['member']).read()
    temporary = dest / (name + '.tmp')
    temporary.write_bytes(data)
    temporary.chmod(0o755)
    os.replace(temporary, dest / name)
    print(name + ' ' + spec['version'] + ': checksum verified')
