"""T-BACKUP safety boundaries can be tested without a cluster."""
import importlib.util
from pathlib import Path
import tarfile
import io

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('maintenance', ROOT / 'scripts/maintenance.py')
maintenance = importlib.util.module_from_spec(spec)
spec.loader.exec_module(maintenance)


@pytest.mark.parametrize('name,kind', [('../escape', tarfile.REGTYPE), ('/escape', tarfile.REGTYPE), ('link', tarfile.SYMTYPE), ('device', tarfile.CHRTYPE)])
def test_restore_rejects_unsafe_archives(tmp_path, name, kind):
    path = tmp_path / 'bad.tgz'
    with tarfile.open(path, 'w:gz') as archive:
        member = tarfile.TarInfo(name)
        member.type = kind
        member.linkname = '/outside'
        archive.addfile(member)
    with pytest.raises(ValueError, match='Unsafe archive'):
        maintenance.validate_archive(path)


def test_restore_accepts_whole_directory_and_hidden_files(tmp_path):
    path = tmp_path / 'good.tgz'
    with tarfile.open(path, 'w:gz') as archive:
        for name in ('./profiles/a/app.db', './.registry-backup', './secrets.vault'):
            member = tarfile.TarInfo(name)
            member.size = 1
            archive.addfile(member, io.BytesIO(b'x'))
    maintenance.validate_archive(path)
