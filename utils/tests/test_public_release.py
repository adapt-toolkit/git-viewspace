"""Public naming, licensing and entry-point regression checks."""
import importlib
from pathlib import Path
import subprocess
import sys
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[2]


class PublicReleaseTests(unittest.TestCase):
    def test_manager_package_metadata(self):
        metadata = tomllib.loads((ROOT / 'pyproject.toml').read_text())
        project = metadata['project']
        self.assertEqual(project['name'], 'git-viewspace')
        self.assertEqual(project['license'], 'Apache-2.0')
        self.assertEqual(project['version'], '0.1.0')
        self.assertEqual(project['scripts'], {
            'git-viewspace': 'git_viewspace.cli:main',
            'git-viewspace-fs': 'git_viewspace_fs.__main__:console_main',
        })
        self.assertEqual(project['dependencies'], ['fusepy>=3.0.1,<4', 'pygit2>=1.20,<1.21'])
        for name in ('git-viewspace', 'git-viewspace-fs'):
            self.assertFalse((ROOT / 'apps' / name / 'pyproject.toml').exists())
            self.assertFalse((ROOT / 'apps' / name / 'setup.py').exists())

    def test_distributions_ship_notices(self):
        for name in ('git-viewspace', 'git-viewspace-fs'):
            directory = ROOT / 'apps' / name
            self.assertEqual((ROOT / 'LICENSE').read_bytes(), (directory / 'LICENSE').read_bytes())
            self.assertEqual((ROOT / 'NOTICE').read_bytes(), (directory / 'NOTICE').read_bytes())
        notice = (ROOT / 'NOTICE').read_text()
        self.assertIn('Vitalis Salis and Diomidis Spinellis', notice)
        self.assertIn('Adapt Framework Solutions', notice)

    def test_manager_help_uses_public_name(self):
        result = subprocess.run(
            [sys.executable, '-m', 'git_viewspace', '--help'],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('git-viewspace', result.stdout)
        self.assertIn('security sandbox', result.stdout)

    def test_public_modules_import(self):
        for name in ('git_viewspace.cli', 'git_viewspace.submodules', 'git_viewspace_fs.gitoper'):
            self.assertIsNotNone(importlib.import_module(name))


if __name__ == '__main__':
    unittest.main()
