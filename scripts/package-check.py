#!/usr/bin/env python3
"""Check source/package contracts without requiring a remote or committed tree."""
import argparse
import hashlib
import pathlib
import posixpath
import re
import subprocess
import tarfile
import tempfile
import yaml

root = pathlib.Path(__file__).resolve().parents[1]
p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--archive', type=pathlib.Path)
a = p.parse_args()
chart = yaml.safe_load((root / 'charts/wealthfolio/Chart.yaml').read_text())
values = yaml.safe_load((root / 'charts/wealthfolio/values.yaml').read_text())
assert chart['appVersion'] == '3.11.0'
assert values['image']['tag'] == '3.11.0'
assert values['image']['repository'] == 'ghcr.io/wealthfolio/wealthfolio'
assert values['image']['digest'] == 'sha256:3e996e9b3fbf9c945eca89e7b89eb03e0966beb1c51b977ccccb811f76ae2994'
fixture = yaml.safe_load((root / 'tests/values/default-valid.yaml').read_text())
assert fixture['image']['digest'] == 'sha256:3e996e9b3fbf9c945eca89e7b89eb03e0966beb1c51b977ccccb811f76ae2994'
for name in ('LICENSE', 'NOTICE'):
    assert (root / name).read_bytes() == (root / 'charts/wealthfolio' / name).read_bytes(), f'Chart {name} differs from repository attribution'
if a.archive:
    with tarfile.open(a.archive) as archive:
        names = archive.getnames()
        required = {'wealthfolio/' + f for f in ('Chart.yaml', 'values.yaml', 'values.schema.json', 'README.md', 'LICENSE', 'NOTICE')}
        assert required <= set(names), 'Missing package metadata: ' + str(required - set(names))
        for member in archive.getmembers():
            assert member.isfile() or member.isdir(), 'Package must not contain links'
            assert member.name.startswith('wealthfolio/') and '..' not in pathlib.PurePosixPath(member.name).parts
            assert not any(x in member.name for x in ('.git/', '.tools/', '.venv/', '.un~', '.env', 'kubeconfig', 'test-evidence', '.key')), 'Unexpected private/editor content'
            if member.isfile() and member.name.endswith('.md'):
                markdown = archive.extractfile(member).read().decode('utf-8')
                for link in re.findall(r'\[[^\]]*\]\(([^)]+)\)', markdown):
                    target = link.split('#', 1)[0]
                    if not target or re.match(r'[a-z]+://|mailto:', target):
                        continue
                    target = posixpath.normpath(posixpath.join(posixpath.dirname(member.name), target))
                    assert target in names, f'Chart documentation link is missing from archive: {link}'
    # Compare logical tar members, not gzip/tar timestamps. Repackaging privately
    # delegates .helmignore handling and Chart.yaml serialization to Helm itself.
    with tempfile.TemporaryDirectory(prefix='wealthfolio-package-check-') as temporary:
        subprocess.run(['helm', 'package', str(root / 'charts/wealthfolio'), '--destination', temporary], check=True, capture_output=True)
        expected = pathlib.Path(temporary) / (chart['name'] + '-' + chart['version'] + '.tgz')
        with tarfile.open(a.archive) as actual_tar, tarfile.open(expected) as expected_tar:
            actual = {m.name: actual_tar.extractfile(m).read() for m in actual_tar.getmembers() if m.isfile()}
            current = {m.name: expected_tar.extractfile(m).read() for m in expected_tar.getmembers() if m.isfile()}
        assert actual == current, 'Package is stale relative to current chart source; freeze source then package once and run fresh package smoke'
    digest = hashlib.sha256(a.archive.read_bytes()).hexdigest()
    a.archive.with_suffix(a.archive.suffix + '.sha256').write_text(digest + '  ' + a.archive.name + '\n')
    print('Package contents verified; SHA256 ' + digest)
