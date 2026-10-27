"""Compact execution roots cannot widen the semantic sync manifest."""
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from git_viewspace.git import Git
from git_viewspace.errors import GitViewspaceError


class StagingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        self.git = Git()
        self.git.create_empty(self.repo, 'main', ('Test', 'test@example.test'))
        self.write('data/edit', 'base')
        self.write('data/delete', 'base')
        self.write('data/to-dir', 'base')
        self.write('data/to-file/child', 'base')
        self.write('data/mode', 'base')
        self.write('data/sibling', 'base')
        self.write('data/edit-neighbor', 'base')
        self.write('.gitignore', '*.ignored\n')
        os.symlink('edit', self.repo / 'data/link')
        self.git._repo(self.repo, ('add', '-A'))
        self.git.run(('-C', self.repo, 'commit', '-qm', 'fixture'), identity=('Test','test@example.test'))
        self.base = self.git.checkout_position(self.repo)[0]

    def write(self, path, content):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)

    def stage(self, manifest, reset=True):
        # Exercise real compact path on small fixtures without a giant test dataset.
        with patch.object(Git, '_staging_roots', return_value=['data']):
            return self.git.stage_manifest(self.repo, self.base, manifest, reset_index=reset)

    def test_native_tree_equivalence_and_repeated_stage(self):
        self.write('data/edit','changed')
        (self.repo/'data/delete').unlink()
        (self.repo/'data/to-dir').unlink()
        self.write('data/to-dir/new','new')
        shutil.rmtree(self.repo/'data/to-file')
        self.write('data/to-file','now a file')
        (self.repo/'data/mode').chmod(0o755)
        (self.repo/'data/link').unlink()
        os.symlink('mode',self.repo/'data/link')
        self.write('data/new.ignored','ignored')
        self.write('data/:literal[*]\nname','literal')
        manifest = ['data/edit','data/delete','data/to-dir/new','data/to-file',
                    'data/mode','data/link','data/new.ignored','data/:literal[*]\nname']
        expected = self.git.stage_manifest(self.repo,self.base,manifest)
        self.git._repo(self.repo, ('reset','--mixed',self.base))
        self.assertEqual(self.stage(manifest,False),expected)
        self.assertEqual(self.stage(manifest),expected)
        self.assertEqual(self.git.unstaged_paths(self.repo),())
        self.assertEqual(self.git.checkout_position(self.repo)[0],self.base)
        self.assertFalse((self.repo/'.git/index.lock').exists())

    def test_unauthorized_changes_preserve_index(self):
        for path in ('data/sibling','data/edit-neighbor','data/extra.ignored'):
            with self.subTest(path=path):
                self.write('data/edit','changed')
                self.write(path,'unauthorized')
                before=(self.repo/'.git/index').read_bytes()
                with self.assertRaises(GitViewspaceError) as caught:
                    self.stage(['data/edit'])
                self.assertEqual(caught.exception.code,'INCOMPLETE_SYNC_MANIFEST')
                self.assertEqual((self.repo/'.git/index').read_bytes(),before)
                self.assertEqual(self.git.checkout_position(self.repo)[0],self.base)
                self.assertFalse((self.repo/'.git/index.lock').exists())
                self.git._repo(self.repo,('reset','--hard',self.base))
                self.git._repo(self.repo,('clean','-fdx'))

    def test_locks_and_git_failure(self):
        self.write('data/edit','changed')
        for name in ('index.lock','index.lock.lock'):
            lock=self.repo/'.git'/name
            lock.write_bytes(b'owned elsewhere')
            before=(self.repo/'.git/index').read_bytes()
            with self.assertRaises(GitViewspaceError): self.stage(['data/edit'])
            self.assertEqual(lock.read_bytes(),b'owned elsewhere')
            self.assertEqual((self.repo/'.git/index').read_bytes(),before)
            lock.unlink()
        before=(self.repo/'.git/index').read_bytes()
        with patch.object(self.git,'_repo_with_environment',side_effect=GitViewspaceError('GIT_FAILED','injected')):
            with self.assertRaises(GitViewspaceError): self.stage(['data/edit'])
        self.assertEqual((self.repo/'.git/index').read_bytes(),before)
        self.assertFalse((self.repo/'.git/index.lock').exists())

    def test_root_selection_and_manifest_boundaries(self):
        sparse=[f'data/{i}/file' for i in range(257)]
        self.assertIs(self.git._staging_roots(sparse),sparse)
        dense=[f'data/{i//100}/file{i}' for i in range(1000)]
        self.assertEqual(len(self.git._staging_roots(dense)),10)
        wide=[f'data/{i}/file' for i in range(10001)]
        self.assertEqual(self.git._staging_roots(wide),['data'])
        self.git._validate_staging_delta(b'D\0a\0A\0a/b\0',['a/b'])
        self.git._validate_staging_delta(b'M\0a/b\0',['a'])
        for delta in (b'M\0ab\0',b'D\0a\0M\0a/b\0'):
            with self.assertRaises(GitViewspaceError):
                self.git._validate_staging_delta(delta,['a/b'])

    def test_install_does_not_remove_next_writers_locks(self):
        self.write('data/edit', 'changed')
        replace = os.replace

        def next_writer(source, destination):
            replace(source, destination)
            (self.repo / '.git/index.lock').write_bytes(b'next writer')
            (self.repo / '.git/index.lock.lock').write_bytes(b'next nested lock')

        with patch('git_viewspace.git.os.replace', side_effect=next_writer):
            self.stage(['data/edit'])
        self.assertEqual((self.repo / '.git/index.lock').read_bytes(), b'next writer')
        self.assertEqual((self.repo / '.git/index.lock.lock').read_bytes(), b'next nested lock')

    def test_failed_restage_preserves_previous_stage(self):
        self.write('data/edit', 'first change')
        self.stage(['data/edit'])
        before = (self.repo / '.git/index').read_bytes()
        self.write('data/sibling', 'unlisted change')
        with self.assertRaises(GitViewspaceError):
            self.stage(['data/edit'])
        self.assertEqual((self.repo / '.git/index').read_bytes(), before)
