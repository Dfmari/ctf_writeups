"""Regression coverage for publishing an exported site without losing local assets."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
import zipfile
from html.parser import HTMLParser
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "prepare_publish.py"
FONT_NAME = "Fixture.woff2"
FONT_BYTES = b"wOF2" + b"fixture-font-data"
CUSTOM_ICON = b"custom-favicon-kept-between-exports"
CSS = (
    '@font-face { font-family: "Fixture Font"; '
    'src: url("./fonts/Fixture.woff2") format("woff2"); }\n'
    'body { font-family: "Fixture Font", sans-serif; }\n'
    '/* Obsidian Webpage Export presentation overrides */\n'
)


def write(path: Path, contents: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(contents, bytes):
        path.write_bytes(contents)
    else:
        path.write_text(contents, encoding="utf-8")


def page(contents: str = "<p>Exported writeup.</p>") -> str:
    return (
        '<!doctype html><html><head><title>Writeup</title><base href="..">'
        '<link rel="icon" href="site-lib/media/favicon.png">'
        '</head><body class="publish"><h1>Writeup</h1>'
        f'{contents}</body></html>'
    )


def metadata(export_path: str, title: str) -> str:
    return json.dumps({
        "files": [{
            "type": "markdown",
            "exportPath": export_path,
            "sourcePath": export_path.removesuffix(".html") + ".md",
            "title": title,
            "createdTime": 1,
        }],
    })


def snapshot(root: Path) -> dict[str, str]:
    result = {}
    for item in root.rglob("*"):
        relative = item.relative_to(root).as_posix()
        result[relative] = (
            "directory" if item.is_dir()
            else hashlib.sha256(item.read_bytes()).hexdigest()
        )
    return result


class AnchorCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.anchors: dict[str, dict[str, str | None]] = {}

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            values = dict(attrs)
            if values.get("href"):
                self.anchors[values["href"]] = values


class PublishRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        # Keep each fixture and its publisher subprocess in a separate directory.
        self.temp_parent = Path(tempfile.gettempdir()).resolve()
        self.temp_root = self.temp_parent / ("ctf-publish-test-" + uuid.uuid4().hex)
        self.temp_root.mkdir(mode=0o755)
        self.repo = self.temp_root / "repo"
        self.export = self.temp_root / "export"
        self.repo.mkdir()
        self.export.mkdir()

        write(self.repo / "index.html", (
            '<!doctype html><html><head><title>Handwritten homepage</title>'
            '<link rel="stylesheet" href="./site.css">'
            '<link rel="icon" href="./site-lib/media/favicon.png">'
            '</head><body data-shell="keep"><h1>HOME SHELL</h1>'
            '<main><p>OLD GENERATED MAIN</p></main></body></html>'
        ))
        write(self.repo / "site.css", CSS)
        write(self.repo / "fonts" / FONT_NAME, FONT_BYTES)
        write(self.repo / ".git" / "config", "repository-config-to-preserve\n")
        write(self.repo / "README.md", "Handwritten README.\n")
        write(self.repo / "prepare_publish.py", "# Repository's publish script.\n")
        write(self.repo / "site-lib" / "metadata.json", metadata("event/old.html", "Old"))
        write(self.repo / "site-lib" / "media" / "favicon.png", CUSTOM_ICON)
        write(self.repo / "event" / "old.html", page())

        write(self.export / "site-lib" / "metadata.json", metadata("event/writeup.html", "Writeup"))
        write(self.export / "site-lib" / "media" / "favicon.png", b"exporter-default-icon")
        write(self.export / "event" / "writeup.html", page())

    def tearDown(self) -> None:
        target = self.temp_root.resolve()
        if target.parent != self.temp_parent or not target.name.startswith("ctf-publish-test-"):
            raise RuntimeError(f"Refusing to remove an unexpected fixture directory: {target}")
        shutil.rmtree(target)

    def run_publish(self, source: Path | None = None, *, repo: Path | None = None,
                    check: bool = False) -> subprocess.CompletedProcess[str]:
        command = [sys.executable, str(SCRIPT)]
        if source is not None:
            command.append(str(source))
        command.extend(["--repo", str(repo or self.repo)])
        if check:
            command.append("--check")
        return subprocess.run(
            command, cwd=self.temp_root, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=30,
        )

    def assert_success(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_persistent_files_and_homepage_shell_survive_export_collisions(self) -> None:
        protected = ["site.css", f"fonts/{FONT_NAME}", ".git/config", "README.md", "prepare_publish.py"]
        expected = {relative: (self.repo / relative).read_bytes() for relative in protected}
        for relative in protected:
            write(self.export / relative, "unwanted export collision")
        write(self.export / "fonts" / "Unwanted.woff2", b"wOF2unwanted")
        write(self.export / "index.html", "<html><head><title>Unwanted export homepage</title></head><body></body></html>")

        self.assert_success(self.run_publish(self.export))

        for relative, contents in expected.items():
            with self.subTest(path=relative):
                self.assertEqual((self.repo / relative).read_bytes(), contents)
        self.assertFalse((self.repo / "fonts" / "Unwanted.woff2").exists())
        homepage = (self.repo / "index.html").read_text(encoding="utf-8")
        self.assertIn("Handwritten homepage", homepage)
        self.assertIn('data-shell="keep"', homepage)
        self.assertIn("HOME SHELL", homepage)
        self.assertIn("event/writeup.html", homepage)
        self.assertNotIn("OLD GENERATED MAIN", homepage)
        self.assertNotIn("Unwanted export homepage", homepage)

    def test_custom_favicon_wins_over_exported_favicon(self) -> None:
        self.assert_success(self.run_publish(self.export))
        self.assertEqual((self.repo / "site-lib" / "media" / "favicon.png").read_bytes(), CUSTOM_ICON)

    def test_omitted_archives_survive_and_bypass_page_routing(self) -> None:
        archives = {
            "event/media/challenge.ZIP": b"zip-attachment",
            "event/media/evidence.7z": b"7z-attachment",
        }
        links = []
        for relative, contents in archives.items():
            write(self.repo / relative, contents)
            links.append(f'<a class="internal-link kept-class" data-href="{relative}" href="{relative}">Download</a>')
        write(self.export / "event" / "writeup.html", page("".join(links)))

        self.assert_success(self.run_publish(self.export))

        collector = AnchorCollector()
        collector.feed((self.repo / "event" / "writeup.html").read_text(encoding="utf-8"))
        for relative, contents in archives.items():
            with self.subTest(path=relative):
                self.assertEqual((self.repo / relative).read_bytes(), contents)
                attrs = collector.anchors[relative]
                self.assertNotIn("internal-link", attrs.get("class", "").split())
                self.assertIn("kept-class", attrs.get("class", "").split())
                self.assertNotIn("data-href", attrs)
                self.assertIn("download", attrs)

    def test_invalid_export_link_fails_without_changing_repo(self) -> None:
        write(self.export / "event" / "writeup.html", page('<img src="event/media/missing.png">'))
        before = snapshot(self.repo)

        result = self.run_publish(self.export)

        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("missing.png", result.stdout + result.stderr)
        self.assertEqual(snapshot(self.repo), before)

    def test_overlapping_roots_are_rejected_without_changes(self) -> None:
        nested_export = self.repo / "incoming-export"
        shutil.copytree(self.export, nested_export)
        nested_repo = self.export / "checkout"
        shutil.copytree(self.repo, nested_repo)
        for source, destination in (
            (self.repo, self.repo),
            (nested_export, self.repo),
            (self.export, nested_repo),
        ):
            with self.subTest(source=source, destination=destination):
                before = snapshot(self.temp_root)
                result = self.run_publish(source, repo=destination)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("must be separate", result.stdout + result.stderr)
                self.assertEqual(snapshot(self.temp_root), before)

    def test_missing_css_font_fails_without_changing_repo(self) -> None:
        write(self.repo / "site.css", CSS.replace("Fixture.woff2", "Missing.woff2"))
        before = snapshot(self.repo)

        result = self.run_publish(self.export)

        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Missing.woff2", result.stdout + result.stderr)
        self.assertEqual(snapshot(self.repo), before)

    def test_otf_font_can_contain_truetype_outlines(self) -> None:
        write(self.repo / "fonts" / "Fixture.otf", b"\x00\x01\x00\x00fixture-font-data")
        write(self.repo / "site.css", CSS.replace("Fixture.woff2", "Fixture.otf").replace('format("woff2")', 'format("opentype")'))

        self.assert_success(self.run_publish(self.export, check=True))

    def test_unsafe_metadata_root_is_rejected_without_changes(self) -> None:
        write(self.repo / "site-lib" / "metadata.json", metadata("../outside.html", "Outside"))
        before = snapshot(self.temp_root)

        result = self.run_publish(self.export)

        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("escapes the repository", result.stdout + result.stderr)
        self.assertEqual(snapshot(self.temp_root), before)

    def test_check_does_not_modify_repo(self) -> None:
        before = snapshot(self.repo)

        self.assert_success(self.run_publish(self.export, check=True))

        self.assertEqual(snapshot(self.repo), before)

    def test_repeated_publish_produces_identical_files(self) -> None:
        self.assert_success(self.run_publish(self.export))
        first = snapshot(self.repo)

        self.assert_success(self.run_publish(self.export))

        self.assertEqual(snapshot(self.repo), first)

    def test_zip_with_loose_root_files_does_not_publish_archive_stash(self) -> None:
        archive_path = "event/media/manual.ZIP"
        write(self.repo / archive_path, b"manual-attachment")
        export_zip = self.temp_root / "Export.zip"
        with zipfile.ZipFile(export_zip, "w") as package:
            for item in self.export.rglob("*"):
                if item.is_file():
                    package.write(item, item.relative_to(self.export).as_posix())

        self.assert_success(self.run_publish(export_zip))

        self.assertEqual((self.repo / archive_path).read_bytes(), b"manual-attachment")
        self.assertFalse((self.repo / "preserved-archives").exists())
        self.assertFalse((self.repo / "event" / "old.html").exists())
        self.assertTrue((self.repo / "event" / "writeup.html").is_file())
        self.assertEqual(
            {item.name for item in self.repo.iterdir()},
            {".git", "event", "fonts", "index.html", "prepare_publish.py", "README.md", "site-lib", "site.css"},
        )


if __name__ == "__main__":
    unittest.main()
