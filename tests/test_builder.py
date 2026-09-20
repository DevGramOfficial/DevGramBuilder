import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
import zipfile

from devgram_builder.cli import BuilderError, build_project, new_project


class WorkingDirectory:
    def __init__(self, path):
        self.path = path

    def __enter__(self):
        self.old = os.getcwd()
        os.chdir(self.path)

    def __exit__(self, *_):
        os.chdir(self.old)


def new_args(directory):
    return argparse.Namespace(
        directory=str(directory), gen=True, name="Test Plugin", author="@DevGram",
        plugin_id="devgram.test", plugin_version="1.2.3", description="Test",
        icon="", force=False,
    )


def build_args(output, compile_level=None):
    return argparse.Namespace(
        no_assets=False, no_folder=True, verbose=False, reset=False,
        ast=compile_level is None, compile=compile_level, no_info=False,
        static_version=None, static_client=None, encrypt=None, output=str(output),
    )


class BuilderTests(unittest.TestCase):
    def test_source_build_is_valid_and_reproducible(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "plugin"
            new_project(new_args(root))
            first = Path(temporary) / "first.dgplugin"
            second = Path(temporary) / "second.dgplugin"
            with WorkingDirectory(root):
                build_project(build_args(first), quiet=True)
                build_project(build_args(second), quiet=True)
            self.assertEqual(hashlib.sha256(first.read_bytes()).digest(), hashlib.sha256(second.read_bytes()).digest())
            with zipfile.ZipFile(first) as archive:
                self.assertIsNone(archive.testzip())
                manifest = json.loads(archive.read("manifest.json"))
                self.assertEqual(manifest["id"], "devgram.test")
                self.assertEqual(manifest["main"], "main.py")
                self.assertIn("locales/ru.json", archive.namelist())

    def test_compiled_build_uses_python_311_entrypoint(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "plugin"
            new_project(new_args(root))
            output = Path(temporary) / "compiled.dgplugin"
            with WorkingDirectory(root):
                try:
                    build_project(build_args(output, 2), quiet=True)
                except BuilderError as error:
                    if "Python 3.11" in str(error):
                        self.skipTest(str(error))
                    raise
            with zipfile.ZipFile(output) as archive:
                manifest = json.loads(archive.read("manifest.json"))
                self.assertEqual(manifest["main"], "main.pyc")
                self.assertIn("main.pyc", archive.namelist())
                self.assertNotIn("main.py", archive.namelist())

    def test_ast_validation_rejects_broken_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "plugin"
            new_project(new_args(root))
            (root / "src" / "main.py").write_text("def broken(:\n", "utf-8")
            with WorkingDirectory(root), self.assertRaises(BuilderError):
                build_project(build_args(Path(temporary) / "broken.dgplugin"), quiet=True)

    def test_aes_protects_sources_but_package_installs_without_password(self):
        import pyzipper

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "plugin"
            new_project(new_args(root))
            output = Path(temporary) / "encrypted.dgplugin"
            args = build_args(output)
            args.encrypt = ["aes-256", "correct-password"]
            with WorkingDirectory(root):
                build_project(args, quiet=True)
            with zipfile.ZipFile(output) as archive:
                manifest = json.loads(archive.read("manifest.json"))
                self.assertEqual(manifest["id"], "devgram.test")
                self.assertEqual(manifest["main"], "main.pyc")
                self.assertIn("main.pyc", archive.namelist())
                self.assertNotIn("main.py", archive.namelist())
                protected = archive.read(".devgram/protected-sources.zip")
            with pyzipper.AESZipFile(io.BytesIO(protected)) as archive:
                with self.assertRaises(RuntimeError):
                    archive.read("main.py")
                archive.setpassword(b"correct-password")
                source = archive.read("main.py").decode("utf-8")
                self.assertIn("class Plugin(BasePlugin)", source)

    def test_encryption_rejects_short_password(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "plugin"
            new_project(new_args(root))
            args = build_args(Path(temporary) / "encrypted.dgplugin")
            args.encrypt = ["aes-256", "short"]
            with WorkingDirectory(root), self.assertRaises(BuilderError):
                build_project(args, quiet=True)


if __name__ == "__main__":
    unittest.main()
