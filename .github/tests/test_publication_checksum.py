import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('verify_checksum', Path(__file__).parents[1] / 'scripts/verify_checksum.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ChecksumTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.archive = Path(self.directory.name) / 'wealthfolio-0.1.0.tgz'
        self.checksum = self.archive.with_suffix('.tgz.sha256')
        self.archive.write_bytes(b'synthetic immutable package bytes')
        self.correct = hashlib.sha256(self.archive.read_bytes()).hexdigest() + '  ' + self.archive.name + '\n'
        self.checksum.write_text(self.correct)

    def test_exact_download_passes(self):
        self.assertTrue(module.verify(self.archive, self.checksum))

    def test_corrupt_download_fails(self):
        self.archive.write_bytes(self.archive.read_bytes() + b'corruption')
        with self.assertRaises(ValueError): module.verify(self.archive, self.checksum)

    def test_wrong_missing_malformed_or_multiple_checksum_records_fail(self):
        for value in ('', 'not-a-sha  ' + self.archive.name, 'a' * 64 + '  ../' + self.archive.name,
                      'a' * 64 + '  different-0.1.0.tgz', self.correct + self.correct,
                      self.correct + '\n'):
            with self.subTest(record=value):
                self.checksum.write_text(value)
                with self.assertRaises(ValueError): module.verify(self.archive, self.checksum)
        self.checksum.unlink()
        with self.assertRaises(FileNotFoundError): module.verify(self.archive, self.checksum)

    def test_symlink_artifact_or_checksum_rejected(self):
        original = self.archive.with_name('original.bin')
        self.archive.rename(original)
        self.archive.symlink_to(original)
        with self.assertRaises(ValueError): module.verify(self.archive, self.checksum)
        self.archive.unlink(); original.rename(self.archive)
        original_checksum = self.checksum.with_name('original.sha256')
        self.checksum.rename(original_checksum); self.checksum.symlink_to(original_checksum)
        with self.assertRaises(ValueError): module.verify(self.archive, self.checksum)

    def test_checksum_must_accompany_exact_basename(self):
        wrong = self.checksum.with_name('other.sha256')
        self.checksum.rename(wrong)
        with self.assertRaises(ValueError): module.verify(self.archive, wrong)


if __name__ == '__main__': unittest.main()
