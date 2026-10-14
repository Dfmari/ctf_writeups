"""Coverage for the publish configuration file and the build_site() entry point."""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
import prepare_publish as pp  # noqa: E402

# The regression suite's fixture helpers (write/page/metadata/snapshot).
_spec = importlib.util.spec_from_file_location(
    "tpp_fixture", REPO_ROOT / "tests" / "test_prepare_publish.py"
)
tpp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tpp)


class WorkdirTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="ctf-config-test-")
        work = Path(self.temp.name)
        self.repo = work / "repo"
        self.export = work / "Export"
        self.repo.mkdir()
        self.export.mkdir()

        tpp.write(self.repo / "index.html", (
            '<!doctype html><html><head><title>Home</title>'
            '<link rel="stylesheet" href="./site.css">'
            '</head><body><main><p>OLD</p></main></body></html>'
        ))
        tpp.write(self.repo / "site.css", tpp.CSS)
        tpp.write(self.repo / "fonts" / tpp.FONT_NAME, tpp.FONT_BYTES)
        tpp.write(self.repo / "site-lib" / "media" / "favicon.png", b"custom-icon")
        tpp.write(self.repo / "publish_config.json", json.dumps(pp.default_config()))

        tpp.write(self.export / "site-lib" / "metadata.json", tpp.metadata("event/writeup.html", "Writeup"))
        tpp.write(self.export / "site-lib" / "media" / "favicon.png", b"exported-icon")
        tpp.write(self.export / "event" / "writeup.html", tpp.page())

    def tearDown(self) -> None:
        self.temp.cleanup()


class ConfigRoundTripTests(WorkdirTestCase):
    def test_missing_file_falls_back_to_embedded_defaults(self) -> None:
        (self.repo / pp.CONFIG_FILE).unlink()
        config = pp.load_config(self.repo)
        self.assertEqual(config["events"], pp.DEFAULT_EVENT_INFO)
        self.assertEqual(config["landing_overrides"], pp.DEFAULT_LANDING_OVERRIDES)

    def test_save_then_load_round_trips(self) -> None:
        config = pp.default_config()
        config["events"]["srdnlenctf"]["note"] = "Web security CTF."
        pp.save_config(self.repo, config)

        self.assertEqual(pp.load_config(self.repo), config)

        # The written file must stay readable by any JSON consumer.
        on_disk = json.loads((self.repo / pp.CONFIG_FILE).read_text(encoding="utf-8"))
        self.assertEqual(on_disk, config)

    def test_invalid_json_is_rejected(self) -> None:
        (self.repo / pp.CONFIG_FILE).write_text("{not json", encoding="utf-8")
        with self.assertRaises(SystemExit):
            pp.load_config(self.repo)

    def test_wrong_field_types_are_rejected(self) -> None:
        config = pp.default_config()
        config["events"]["srdnlenctf"]["default_tags"] = "Web"
        with self.assertRaises(SystemExit):
            pp.validate_config(config)

        config = pp.default_config()
        config["landing_overrides"]["tool-setup.html"]["tags"] = [1, 2]
        with self.assertRaises(SystemExit):
            pp.validate_config(config)

    def test_gui_config_shape_is_valid(self) -> None:
        # The GUI posts exactly this shape back to /api/config.
        pp.validate_config(pp.default_config())  # must not raise


class BuildSiteTests(WorkdirTestCase):
    """build_site() must expose what the GUI needs without changing behavior."""

    def test_check_mode_returns_pages_and_leaves_repo_alone(self) -> None:
        before = tpp.snapshot(self.repo)
        result = pp.build_site(self.repo, self.export, check=True, log=lambda *_: None)

        self.assertEqual(result.errors, [])
        self.assertEqual(result.pages, ["event/writeup.html"])
        self.assertEqual(tpp.snapshot(self.repo), before)

    def test_publish_reports_changed_files(self) -> None:
        result = pp.build_site(self.repo, self.export, log=lambda *_: None)
        self.assertEqual(result.errors, [])
        self.assertIn("event", result.changed_files)
        self.assertTrue((self.repo / "event" / "writeup.html").is_file())

    def test_errors_are_returned_not_raised(self) -> None:
        # A page linking to a file the export does not provide fails validation.
        tpp.write(
            self.export / "event" / "writeup.html",
            tpp.page('<img src="missing-image.png">'),
        )
        result = pp.build_site(self.repo, self.export, check=True, log=lambda *_: None)
        self.assertTrue(result.errors)
        self.assertIn("missing-image.png", result.errors[0])

    def test_keep_staging_provides_preview_tree_without_writing_repo(self) -> None:
        before = tpp.snapshot(self.repo)
        result = pp.build_site(self.repo, self.export, keep_staging=True, log=lambda *_: None)
        try:
            self.assertEqual(result.errors, [])
            self.assertIsNotNone(result.staged_site)
            self.assertTrue((result.staged_site / "index.html").is_file())
            # The live repository must be untouched in preview mode.
            self.assertEqual(tpp.snapshot(self.repo), before)
        finally:
            if result.staged_site is not None:
                shutil.rmtree(result.staged_site.parent, ignore_errors=True)

    def test_landing_page_uses_config_overrides(self) -> None:
        config = pp.default_config()
        config["landing_overrides"]["event/writeup.html"] = {
            "title": "Custom Title",
            "description": "Custom description.",
            "tags": ["Web"],
        }
        pp.save_config(self.repo, config)

        result = pp.build_site(self.repo, self.export, log=lambda *_: None)
        self.assertEqual(result.errors, [])
        index = (self.repo / "index.html").read_text(encoding="utf-8")
        self.assertIn("Custom Title", index)
        self.assertIn("Custom description.", index)


class PageEntryTests(WorkdirTestCase):
    def test_entries_merge_overrides_with_fallbacks(self) -> None:
        config = pp.default_config()
        config["events"]["event"] = {"name": "Event", "note": "", "default_tags": ["Misc"]}
        config["landing_overrides"]["event/writeup.html"] = {
            "title": "Overridden",
            "description": "Overridden description.",
            "tags": ["Tagged"],
        }

        entries = pp.page_entries(self.export, config)
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["path"], "event/writeup.html")
        self.assertEqual(entry["override"]["title"], "Overridden")
        self.assertEqual(entry["fallback"]["title"], "Writeup")
        self.assertEqual(entry["fallback"]["tags"], ["Misc"])

    def test_entries_without_override_are_none(self) -> None:
        entries = pp.page_entries(self.export, pp.default_config())
        self.assertIsNone(entries[0]["override"])


if __name__ == "__main__":
    unittest.main()
