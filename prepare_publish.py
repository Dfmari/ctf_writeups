#!/usr/bin/env python3
"""
prepare_publish.py

Merge an Obsidian Webpage HTML Export into this repo and make it publish-ready.

Run from the repository root:

    python prepare_publish.py
    python prepare_publish.py ../../Export
    python prepare_publish.py ~/Downloads/Export.zip
    python prepare_publish.py --check
    python prepare_publish.py --gui        # local web UI (needs: pip install flask)

With no argument, it defaults to ../../Export, which matches:

    Writeups/
    ├── Export/
    └── Git/
        └── ctf_writeups/   <- run it here

The merged site is checked in a temporary directory before replacing output.
--check validates without changing the repository. This script does NOT commit or push.
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import logging.handlers
import posixpath
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.parse
import zipfile
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path


DEFAULT_EXPORT = Path("../../Export")

PERSISTENT_TOP_LEVEL = {
    ".git",
    ".gitignore",
    "index.html",
    "README.md",
    "site.css",
    "fonts",
    "prepare_publish.py",
    "publish_config.json",
    "tests",
}

PERSISTENT_GENERATED_FILES = ("site-lib/media/favicon.png",)
ARCHIVE_SUFFIXES = {".zip", ".7z", ".rar", ".tar", ".tgz", ".gz", ".bz2", ".xz"}


_PERSISTENT_FOLDED = frozenset(entry.casefold() for entry in PERSISTENT_TOP_LEVEL)


def is_persistent(name: str) -> bool:
    return name.casefold() in _PERSISTENT_FOLDED


CONFIG_FILE = "publish_config.json"
LOG_FILE = ".publish-gui.log"

DEFAULT_EVENT_INFO = {
    "belkactf-№7": {
        "name": "BelkaCTF №7",
        "note": "DFIR series revisiting the live CTF with freely available tools.",
        "default_tags": ["DFIR"],
    },
    "srdnlenctf": {
        "name": "SrdnlenCTF",
        "note": "",
        "default_tags": [],
    },
}

DEFAULT_LANDING_OVERRIDES = {
    "tool-setup.html": {
        "title": "Tool Setup",
        "description": "Analysis environment, installation notes, and setup guides for tools used across the writeups.",
        "tags": ["Setup", "Tools"],
    },
    "belkactf-№7/main.html": {
        "title": "Series index",
        "description": "Challenge list, and series notes.",
        "tags": ["DFIR", "Windows"],
    },
    "belkactf-№7/whoami/whoami-baby.html": {
        "title": "Whoami",
        "description": "Recovering the interactive username and hostname from a RAM dump with MemProcFS.",
        "tags": ["Memory", "MemProcFS"],
    },
    "belkactf-№7/locker/locker-warmup.html": {
        "title": "Locker",
        "description": "Finding and validating a rogue executable in memory using MemProcFS.",
        "tags": ["Malware", "MemProcFS"],
    },
    "srdnlenctf/msnrevive/msnrevive.html": {
        "title": "MSN Revive",
        "description": "Missing authorization combined with a path canonicalization/parser differential.",
        "tags": ["Web", "API", "Burp Suite"],
    },
}


def default_config() -> dict:
    return {
        "events": deepcopy(DEFAULT_EVENT_INFO),
        "landing_overrides": deepcopy(DEFAULT_LANDING_OVERRIDES),
    }


def _validate_entry(kind: str, key: str, entry: object, fields: dict) -> None:
    if not isinstance(entry, dict):
        die(f"{CONFIG_FILE}: {kind} {key!r} must be an object.")
    for name, expected in fields.items():
        value = entry.get(name)
        if expected is list:
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                die(f"{CONFIG_FILE}: {kind} {key!r} field {name!r} must be a list of strings.")
        elif not isinstance(value, expected):
            die(f"{CONFIG_FILE}: {kind} {key!r} field {name!r} must be a {expected.__name__}.")


def validate_config(config: dict) -> None:
    if not isinstance(config, dict):
        die("Config must be a JSON object.")
    for section in ("events", "landing_overrides"):
        if not isinstance(config.get(section), dict):
            die(f"Config: missing or invalid {section!r} object.")
    for key, entry in config["events"].items():
        _validate_entry("event", key, entry, {"name": str, "note": str, "default_tags": list})
    for key, entry in config["landing_overrides"].items():
        _validate_entry("override", key, entry, {"title": str, "description": str, "tags": list})


def load_config(repo: Path) -> dict:
    """
    Read publish_config.json from the repository root.

    The file is optional: without it the embedded defaults above are used, so
    the repository keeps working before the first GUI/CLI run writes it out.
    """
    path = repo / CONFIG_FILE
    if not path.exists():
        return default_config()

    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        die(f"{path} is not valid JSON: {error}")

    validate_config(config)
    return config


def save_config(repo: Path, config: dict) -> Path:
    """Persist the configuration; writes are atomic so a crash cannot corrupt it."""
    path = repo / CONFIG_FILE
    text = json.dumps(config, indent=2, ensure_ascii=False) + "\n"
    temp = path.with_suffix(".json.tmp")
    temp.write_text(text, encoding="utf-8")
    temp.replace(path)
    return path


def die(message: str) -> None:
    raise SystemExit(f"[!] {message}")


def setup_logging(log_file: Path | None, verbose: bool = False) -> logging.Logger:
    """
    Configure the GUI's file log.

    The log records every API request, the build lifecycle, and browser
    beacons, so a silent UI can be diagnosed after the fact.
    """
    logger = logging.getLogger("prepare_publish.gui")
    logger.setLevel(logging.DEBUG)
    if not logger.handlers and log_file is not None:
        handler = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=1_000_000, backupCount=2, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
        handler.setLevel(logging.DEBUG if verbose else logging.INFO)
        logger.addHandler(handler)
    return logger


def find_export_root(path: Path, temp_dir: Path) -> Path:
    if path.is_dir():
        candidate = path.resolve()
    elif path.is_file() and path.suffix.lower() == ".zip":
        temp_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path) as zf:
            zf.extractall(temp_dir)
        children = [p for p in temp_dir.iterdir() if p.name != "__MACOSX"]
        candidate = children[0] if len(children) == 1 and children[0].is_dir() else temp_dir
    else:
        die(f"Export must be a directory or .zip: {path}")

    if not (candidate / "site-lib").is_dir():
        die(f"{candidate} does not look like a Webpage HTML Export: site-lib/ is missing.")

    return candidate


def read_metadata(root: Path) -> dict:
    path = root / "site-lib" / "metadata.json"
    if not path.exists():
        die("site-lib/metadata.json is missing from the export.")
    return json.loads(path.read_text(encoding="utf-8"))


def webpage_records(metadata: dict) -> list[dict]:
    records = []
    seen = set()

    def walk(obj):
        if isinstance(obj, dict):
            export_path = obj.get("exportPath")
            if (
                isinstance(export_path, str)
                and export_path.endswith(".html")
                and obj.get("type") == "markdown"
            ):
                key = (export_path, obj.get("sourcePath"))
                if key not in seen:
                    seen.add(key)
                    records.append(obj)
            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for value in obj:
                walk(value)

    walk(metadata)
    return records


def old_generated_roots(repo: Path) -> set[str]:
    roots = {"site-lib"}
    metadata_path = repo / "site-lib" / "metadata.json"
    if not metadata_path.exists():
        return roots

    try:
        for rec in webpage_records(json.loads(metadata_path.read_text(encoding="utf-8"))):
            path = rec.get("exportPath", "")
            if "/" in path:
                roots.add(path.split("/", 1)[0])
            elif path and not is_persistent(path):
                roots.add(path)
    except Exception:
        pass

    return roots



def stash_manual_archives(repo: Path, temp_root: Path, roots: set[str]) -> tuple[Path, list[Path]]:
    """
    Preserve manually-kept archive attachments across exporter refreshes.

    Obsidian Webpage Export may omit files such as challenge .zip attachments.
    The generated event directory is rebuilt on each run, so without this stash
    those files can disappear even though the writeup still links to them.
    """
    stash_root = temp_root / "preserved-archives"
    preserved: list[Path] = []

    # Only generated roots are cleared, so archives elsewhere are never at risk.
    candidates = (
        archive
        for name in sorted(roots)
        if (repo / name).is_dir()
        for archive in (repo / name).rglob("*")
    )
    for archive in candidates:
        if not archive.is_file() or archive.suffix.lower() not in ARCHIVE_SUFFIXES:
            continue
        rel = archive.relative_to(repo)

        dest = stash_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(archive, dest)
        preserved.append(rel)

    return stash_root, preserved


def restore_manual_archives(
    repo: Path,
    stash_root: Path,
    preserved: list[Path],
) -> int:
    """
    Restore preserved archives only when the new export did not provide them.
    """
    restored = 0

    for rel in preserved:
        dest = repo / rel
        if dest.exists():
            continue

        src = stash_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        restored += 1

    return restored


def generated_roots(repo: Path, export_root: Path) -> set[str]:
    roots = old_generated_roots(repo)

    for child in export_root.iterdir():
        if not is_persistent(child.name):
            roots.add(child.name)

    return {name for name in roots if not is_persistent(name)}


def checked_generated_targets(repo: Path, roots: set[str]) -> list[Path]:
    repo = repo.resolve()
    targets = []
    for name in sorted(roots):
        target = repo / name
        resolved = target.resolve()
        if (
            name in {"", ".", ".."}
            or "/" in name or "\\" in name or ":" in name
            or resolved == repo or not resolved.is_relative_to(repo)
        ):
            die(f"Generated path escapes the repository: {name!r}")
        targets.append(target)
    return targets


def clear_generated_export(repo: Path, export_root: Path) -> None:
    # Resolve and check every target before deleting any generated output.
    targets = checked_generated_targets(repo, generated_roots(repo, export_root))

    for target in targets:
        if target.is_symlink():
            target.unlink()
        elif target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()


def overlay_export(export_root: Path, repo: Path) -> None:
    for child in export_root.iterdir():
        if is_persistent(child.name):
            continue
        dest = repo / child.name
        if child.is_dir():
            shutil.copytree(child, dest, dirs_exist_ok=True)
        else:
            shutil.copy2(child, dest)



_CLASS_ATTR_RE = re.compile(r'\s+class=["\'](?P<classes>[^"\']*)["\']', re.IGNORECASE)
_DATA_HREF_RE = re.compile(r'\s+data-href=["\'][^"\']*["\']', re.IGNORECASE)
_HREF_RE = re.compile(r'(?<!data-)\bhref=["\'](?P<href>[^"\']+)["\']', re.IGNORECASE)
_ANCHOR_RE = re.compile(r'<a\b(?P<attrs>[^>]*)>(?P<body>.*?)</a>', re.IGNORECASE | re.DOTALL)
_ARCHIVE_EXTS = "(?:" + "|".join(re.escape(ext[1:]) for ext in sorted(ARCHIVE_SUFFIXES)) + ")"
_ARCHIVE_ANCHOR_RE = re.compile(
    rf'<a\b(?P<attrs>[^>]*\bhref=["\'][^"\']+\.{_ARCHIVE_EXTS}'
    rf'(?:[?#][^"\']*)?["\'][^>]*)>',
    re.IGNORECASE,
)
_HOME_TARGETS = {"index.html", "git/ctf_writeups/index.html"}


def drop_internal_link_class(attrs: str) -> tuple[str, bool]:
    """Remove Obsidian's `internal-link` class; report whether it was present."""
    match = _CLASS_ATTR_RE.search(attrs)
    if not match:
        return attrs, False
    classes = match.group("classes").split()
    kept = [c for c in classes if c.lower() != "internal-link"]
    if len(kept) == len(classes):
        return attrs, False
    replacement = f' class="{" ".join(kept)}"' if kept else ""
    return attrs[:match.start()] + replacement + attrs[match.end():], True


def deintercept_attachment_links(text: str) -> str:
    """
    Obsidian Webpage Export marks local archive links as `internal-link`.
    webpage.js intercepts those clicks and asks its page metadata whether the
    target is an exported document. Raw .zip/.7z/etc. files are not documents,
    so it shows "This page does not exist yet" without ever issuing an HTTP GET.

    For downloadable archive attachments, remove the Obsidian interception
    markers and let the browser follow the real href directly.
    """
    def fix_anchor(match: re.Match) -> str:
        attrs = match.group("attrs")

        # External/raw links that are not Obsidian internal links are fine.
        if not _CLASS_ATTR_RE.search(attrs):
            return match.group(0)

        # webpage.js uses data-href/internal-link for Obsidian navigation.
        attrs, was_internal = drop_internal_link_class(_DATA_HREF_RE.sub("", attrs))
        if not was_internal:
            return match.group(0)

        if not re.search(r'\sdownload(?:\s|=|$)', attrs, flags=re.IGNORECASE):
            attrs += " download"

        return f"<a{attrs}>"

    return _ARCHIVE_ANCHOR_RE.sub(fix_anchor, text)



def normalize_home_links(text: str) -> str:
    """
    Obsidian may resolve [Home](index.html) to the real repository file inside
    the vault, exporting it as git/ctf_writeups/index.html. That Git directory
    is intentionally excluded from the published site.

    Rewrite those anchors to the publish-root index.html and remove Obsidian's
    internal-link routing so webpage.js cannot reject the handwritten landing
    page for being absent from exporter metadata.
    """
    def fix_anchor(match: re.Match) -> str:
        attrs = match.group("attrs")
        href_match = _HREF_RE.search(attrs)
        if not href_match:
            return match.group(0)

        href = urllib.parse.unquote(href_match.group("href")).replace("\\", "/")
        if href.split("#", 1)[0].split("?", 1)[0].lstrip("./").lower() not in _HOME_TARGETS:
            return match.group(0)

        attrs = _HREF_RE.sub('href="index.html"', attrs, count=1)
        attrs, _ = drop_internal_link_class(_DATA_HREF_RE.sub("", attrs))
        return f"<a{attrs}>{match.group('body')}</a>"

    return _ANCHOR_RE.sub(fix_anchor, text)


_MISSING_ATTACHMENT_RES = (
    re.compile(r'<span\b(?=[^>]*\bmod-empty-attachment\b)[^>]*>.*?</span>', re.IGNORECASE | re.DOTALL),
    re.compile(r'<span\b(?=[^>]*\bsrc=["\']\.html["\'])[^>]*>.*?</span>', re.IGNORECASE | re.DOTALL),
)
_EMPTY_PARSED_FEATURE_RE = re.compile(
    r'<div\b[^>]*class=["\'][^"\']*\bparsed-feature-container\b[^"\']*["\'][^>]*>\s*</div>',
    re.IGNORECASE,
)
_TEMPLATER_CURSOR_RE = re.compile(r'(?:&lt;|<)%\s*tp\.file\.cursor\(\d+\)\s*%(?:&gt;|>)', re.IGNORECASE)


def remove_missing_attachment_placeholders(text: str) -> str:
    """
    Remove Obsidian's generated placeholder for missing embeds. A missing image
    can otherwise become src=".html", which fails publish validation.
    """
    for pattern in _MISSING_ATTACHMENT_RES:
        text = pattern.sub("", text)
    return text


# Exporter chrome that only makes sense with Obsidian's own CSS and webpage.js.
_CHROME_RES = (
    re.compile(r'<script\b.*?</script>', re.IGNORECASE | re.DOTALL),
    re.compile(r'<style\b[^>]*\bid=["\']MJX-CHTML-styles["\'][^>]*>.*?</style>', re.IGNORECASE | re.DOTALL),
    re.compile(r'<button\b[^>]*\bclass=["\'][^"\']*\bcopy-code-button\b[^"\']*["\'][^>]*>.*?</button>', re.IGNORECASE | re.DOTALL),
    re.compile(r'<span\b[^>]*\bclass=["\'][^"\']*\b(?:collapse-indicator|list-bullet)\b[^"\']*["\'][^>]*>.*?</span>', re.IGNORECASE | re.DOTALL),
    re.compile(r'<div\b[^>]*\bclass=["\']data-bar["\'][^>]*>\s*</div>', re.IGNORECASE),
    re.compile(r'<div\b[^>]*\bclass=["\']footer["\'][^>]*>\s*</div>', re.IGNORECASE),
)
_HEAD_TAG_RE = re.compile(r'<(title)\b[^>]*>(.*?)</title>|<(base|meta|link)\b([^>]*)>', re.IGNORECASE | re.DOTALL)
_DIV_TOKEN_RE = re.compile(r'<div\b[^>]*>|</div\s*>', re.IGNORECASE)

# webpage.js is gone, so folding callouts needs this much script and no more.
_CALLOUT_SCRIPT = """<script>
for (const title of document.querySelectorAll(".callout.is-collapsible > .callout-title")) {
  title.addEventListener("click", () => {
    const callout = title.parentElement;
    const collapsed = callout.classList.toggle("is-collapsed");
    const body = callout.querySelector(":scope > .callout-content");
    if (body) body.style.display = collapsed ? "none" : "";
    const fold = title.querySelector(".callout-fold");
    if (fold) fold.classList.toggle("is-collapsed", collapsed);
  });
}
</script>"""


def _attr(attrs: str, name: str) -> str | None:
    match = re.search(rf'\b{re.escape(name)}=["\']([^"\']*)["\']', attrs, re.IGNORECASE)
    return html.unescape(match.group(1)) if match else None


def _element_inner(text: str, open_tag: re.Match) -> str | None:
    """Return the inner HTML of the <div> opened by `open_tag`, matching nested divs."""
    depth = 1
    for token in _DIV_TOKEN_RE.finditer(text, open_tag.end()):
        depth += -1 if token.group(0).startswith("</") else 1
        if depth == 0:
            return text[open_tag.end():token.start()]
    return None


def _article(text: str) -> str:
    """The page's own content: the preview sizer, else the document, else the body."""
    for cls in ("markdown-preview-sizer", "obsidian-document"):
        open_tag = re.search(rf'<div\b[^>]*\bclass=["\'][^"\']*\b{cls}\b[^"\']*["\'][^>]*>', text, re.IGNORECASE)
        if open_tag:
            inner = _element_inner(text, open_tag)
            if inner is not None:
                return inner
    body = re.search(r'<body\b[^>]*>(.*?)</body>', text, re.IGNORECASE | re.DOTALL)
    return body.group(1) if body else text


def blank_slate_page(text: str) -> str:
    """
    Rebuild an exported page as plain HTML that depends on site.css alone.

    Obsidian Webpage Export wraps every note in its app shell: 250 KB of
    Obsidian CSS, webpage.js, sidebars and a navbar. None of that is needed to
    read a page, and all of it fights with the site stylesheet. Keep the
    document's own markup (headings, callouts, code, embeds, with their
    classes) so site.css has stable hooks, and drop everything else.
    """
    title = description = base = ""
    icon = "site-lib/media/favicon.png"
    head = re.search(r'<head\b[^>]*>(.*?)</head>', text, re.IGNORECASE | re.DOTALL)
    for match in _HEAD_TAG_RE.finditer(head.group(1) if head else ""):
        if match.group(1):
            title = html.unescape(re.sub(r"<[^>]+>", "", match.group(2))).strip()
            continue
        tag, attrs = match.group(3).lower(), match.group(4)
        if tag == "base" and _attr(attrs, "href"):
            base = _attr(attrs, "href")
        elif tag == "meta" and (_attr(attrs, "name") or "").lower() == "description":
            description = _attr(attrs, "content") or ""
        elif tag == "link" and (_attr(attrs, "rel") or "").lower() == "icon" and _attr(attrs, "href"):
            icon = _attr(attrs, "href")

    article = _article(text)
    for pattern in _CHROME_RES:
        article = pattern.sub("", article)
    article = re.sub(
        r'(<div\b[^>]*\bclass=["\']callout-fold[^"\']*["\'][^>]*>\s*<svg\b[^>]*?)\bwidth="24"\s+height="24"',
        r'\1width="1em" height="1em"',
        article,
        flags=re.IGNORECASE,
    )
    article = article.strip()

    lines = [
        "<!DOCTYPE html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{html.escape(title)}</title>",
    ]
    if description:
        lines.append(f'<meta name="description" content="{html.escape(description, quote=True)}">')
    if base:
        lines.append(f'<base href="{html.escape(base, quote=True)}">')
    lines += [
        f'<link rel="icon" href="{html.escape(icon, quote=True)}">',
        '<link rel="stylesheet" href="site.css">',
        "</head>",
        '<body class="publish">',
        '<main class="markdown-preview-view markdown-rendered is-readable-line-width">',
        '<div class="markdown-preview-sizer">',
        article,
        "</div>",
        "</main>",
        _CALLOUT_SCRIPT if "is-collapsible" in article else "",
        "</body>",
        "</html>",
        "",
    ]
    return "\n".join(line for line in lines if line != "")


# With <base> set to the site root, a bare "#id" link resolves against the
# root rather than this page (Obsidian's webpage.js used to intercept these).
# Prefix same-page fragment links with the page's own root-relative path.
_FRAGMENT_ANCHOR_RE = re.compile(r'(<a\b[^>]*\bhref=["\'])#', re.IGNORECASE)


def fix_fragment_links(text: str, page_path: str) -> str:
    return _FRAGMENT_ANCHOR_RE.sub(lambda m: m.group(1) + page_path + "#", text)


def clean_page(text: str, page_path: str) -> str:
    # Strip the exporter's app shell; what remains is content plus site.css.
    text = blank_slate_page(text)

    # Remove now-empty parsed-feature wrappers.
    text = _EMPTY_PARSED_FEATURE_RE.sub("", text)

    # Do not publish forgotten Templater cursor markers.
    text = _TEMPLATER_CURSOR_RE.sub("", text)

    # Normalize Home links that Obsidian resolved into the excluded Git folder.
    text = normalize_home_links(text)

    # Remove missing-attachment placeholders such as Locker.png -> src=".html".
    text = remove_missing_attachment_placeholders(text)

    # Raw archive attachments must bypass Obsidian's internal-page router.
    text = deintercept_attachment_links(text)

    # Same-page anchors must survive the <base> tag.
    return fix_fragment_links(text, page_path)


def clean_exported_html(repo: Path) -> None:
    for page in repo.rglob("*.html"):
        if page == repo / "index.html":
            continue

        original = page.read_text(encoding="utf-8", errors="replace")
        text = clean_page(original, page.relative_to(repo).as_posix())
        if text != original:
            page.write_text(text, encoding="utf-8")

    # Nothing references the exporter's runtime any more; only metadata.json
    # (read to build the landing page) and media/ (favicon, icons) stay.
    for name in ("styles", "scripts", "fonts", "html"):
        shutil.rmtree(repo / "site-lib" / name, ignore_errors=True)


def quote_path(path: str) -> str:
    return "./" + "/".join(
        urllib.parse.quote(part, safe="-._~")
        for part in path.split("/")
    )


def card(title: str, path: str, description: str, tags: list[str]) -> str:
    desc_html = (
        f'\n            <div class="muted">{html.escape(description)}</div>'
        if description else ""
    )

    tags_html = ""
    if tags:
        spans = "\n".join(
            f'            <span class="tag">{html.escape(tag)}</span>'
            for tag in tags
        )
        tags_html = f"""
          <div class="tags">
{spans}
          </div>"""

    return f"""        <li>
          <div class="entry">
            <a href="{quote_path(path)}">{html.escape(title)}</a>{desc_html}
          </div>{tags_html}
        </li>"""


def fallback_title(rec: dict) -> str:
    title = (rec.get("title") or Path(rec.get("exportPath", "")).stem).strip()

    # Strip difficulty suffixes where the source title uses "Challenge - Baby/Warmup".
    if " - " in title:
        title = title.split(" - ", 1)[0].strip()

    # Avoid shouting on the landing page.
    if title.isupper() and len(title) > 1:
        title = title.title()

    return title


def fallback_description(rec: dict, event_name: str) -> str:
    description = (rec.get("description") or "").strip()
    description = re.sub(r"<[^>]+>", "", description)
    description = re.sub(r"\s+", " ", description).strip()

    if description and len(description) <= 180:
        return description

    return f"{event_name} writeup."


def build_landing_main(metadata: dict, config: dict) -> str:
    records = webpage_records(metadata)
    by_path = {rec["exportPath"]: rec for rec in records if rec.get("exportPath")}
    landing_overrides = config["landing_overrides"]
    event_info = config["events"]

    sections = []

    # Resources
    if "tool-setup.html" in by_path:
        ov = landing_overrides.get("tool-setup.html", {})
        sections.append(f"""    <section>
      <h2>&gt; ./resources/</h2>

      <ul>
{card(ov.get("title") or "Tool Setup", "tool-setup.html", ov.get("description", ""), list(ov.get("tags", [])))}
      </ul>
    </section>""")

    grouped: dict[str, list[dict]] = {}
    for rec in records:
        path = rec.get("exportPath", "")
        if path == "tool-setup.html" or "/" not in path:
            continue

        top = path.split("/", 1)[0]
        if top in {"site-lib", "fonts"}:
            continue

        grouped.setdefault(top, []).append(rec)

    for top, recs in grouped.items():
        info = event_info.get(top, {})
        event_name = info.get("name") or top.replace("-", " ").title()
        event_note = info.get("note", "")
        default_tags = info.get("default_tags", [])

        # Creation order tracks the actual series progression well and means a
        # newly-created task naturally appears after the older tasks.
        recs.sort(
            key=lambda r: (
                0 if r.get("exportPath", "").endswith("/main.html") else 1,
                r.get("createdTime", 0),
                r.get("exportPath", ""),
            )
        )

        cards = []
        for rec in recs:
            path = rec["exportPath"]

            # Never expose generated media wrappers as writeups.
            if "/media/" in path:
                continue

            override = landing_overrides.get(path)
            if override:
                title = override["title"]
                description = override["description"]
                tags = override["tags"]
            else:
                title = fallback_title(rec)
                description = fallback_description(rec, event_name)
                tags = list(default_tags)

            cards.append(card(title, path, description, tags))

        if not cards:
            continue

        note_html = (
            f'\n      <p class="event-note">\n        {html.escape(event_note)}\n      </p>\n'
            if event_note else "\n"
        )

        sections.append(f"""    <section>
      <h2>&gt; ./{html.escape(event_name)}/</h2>{note_html}
      <ul>
{chr(10).join(cards)}
      </ul>
    </section>""")

    return "  <main>\n" + "\n\n".join(sections) + "\n  </main>"


def update_index(repo: Path, metadata: dict, config: dict) -> None:
    index = repo / "index.html"
    if not index.exists():
        die("index.html is missing from the repo.")

    text = index.read_text(encoding="utf-8")

    if 'href="./site.css"' not in text and 'href="site.css"' not in text:
        text = text.replace("</title>", '</title>\n  <link rel="stylesheet" href="./site.css">', 1)

    if 'rel="icon"' not in text:
        text = text.replace(
            "</title>",
            '</title>\n  <link rel="icon" href="./site-lib/media/favicon.png">',
            1,
        )

    # The replacement starts at <main>; its existing leading indentation stays.
    new_main = build_landing_main(metadata, config).lstrip()

    if not re.search(r"<main\b[^>]*>.*?</main>", text, flags=re.DOTALL | re.IGNORECASE):
        die("Could not find <main>...</main> in index.html.")

    text = re.sub(
        r"<main\b[^>]*>.*?</main>",
        new_main,
        text,
        count=1,
        flags=re.DOTALL | re.IGNORECASE,
    )

    index.write_text(text, encoding="utf-8")



def ensure_wide_writeups(repo: Path) -> None:
    """
    Apply persistent presentation overrides for exported writeup pages:
    - widen Obsidian's readable-line-length
    - center images responsively

    site.css is preserved across exports and injected into every exported page.
    """
    site_css = repo / "site.css"
    if not site_css.exists():
        die(f"{site_css} is missing.")

    marker = "Obsidian Webpage Export presentation overrides"
    css = site_css.read_text(encoding="utf-8")

    # Remove the older one-purpose block if it exists, so upgrades are idempotent.
    css = re.sub(
        r'/\* Obsidian Webpage Export readable-line-width override \*/.*?'
        r'\.markdown-preview-view\.is-readable-line-width \.markdown-preview-sizer \{.*?\}\s*',
        "",
        css,
        flags=re.DOTALL,
    )

    if marker in css:
        return

    presentation_css = """
/* Obsidian Webpage Export presentation overrides */
body {
  --file-line-width: min(1100px, calc(100vw - 2rem)) !important;
  --line-width: min(1100px, calc(100vw - 2rem)) !important;
  --line-width-adaptive: min(1100px, calc(100vw - 2rem)) !important;
}

.markdown-preview-view.is-readable-line-width .markdown-preview-sizer {
  max-width: min(1100px, calc(100vw - 2rem)) !important;
  width: 100% !important;
}

/* Center normal writeup images without stretching them past their source size. */
.markdown-rendered img,
.markdown-preview-view img,
.markdown-preview-sizer img {
  display: block;
  max-width: 100%;
  height: auto;
  margin-left: auto !important;
  margin-right: auto !important;
}
"""

    site_css.write_text(
        css.rstrip() + "\n\n" + presentation_css.strip() + "\n",
        encoding="utf-8",
    )



def verify_fonts(repo: Path) -> None:
    """Validate the self-hosted fonts actually referenced by the shared CSS."""
    css = (repo / "site.css").read_text(encoding="utf-8")
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    font_faces = re.findall(r"@font-face\s*\{(.*?)\}", css, flags=re.DOTALL | re.IGNORECASE)
    signatures = {
        ".woff2": (b"wOF2",),
        ".woff": (b"wOFF",),
        ".ttf": (b"\x00\x01\x00\x00", b"OTTO", b"ttcf"),
        ".otf": (b"\x00\x01\x00\x00", b"OTTO", b"ttcf"),
    }
    checked = set()
    for face in font_faces:
        for match in re.finditer(r"url\(\s*(?:\"([^\"]+)\"|'([^']+)'|([^\s)]+))\s*\)", face, re.IGNORECASE):
            ref = next(group for group in match.groups() if group is not None)
            url = urllib.parse.urlsplit(ref)
            if url.scheme or url.netloc:
                continue
            font = (repo / urllib.parse.unquote(url.path)).resolve()
            if not font.is_relative_to(repo.resolve()):
                die(f"Font URL leaves the publish directory: {ref}")
            if font in checked:
                continue
            checked.add(font)
            if not font.is_file():
                die(f"Font referenced in site.css is missing: {ref}")
            with font.open("rb") as handle:
                header = handle.read(4)
            if header not in signatures.get(font.suffix.lower(), ()):
                die(f"Font referenced in site.css has an invalid font signature: {ref}")


# Fast tag/attribute scanner. It is ~10x quicker than html.parser on large
# exports and yields the same <base>/href/src values; comments and the bodies
# of <script>/<style> are skipped so their text is never mistaken for markup.
_TAG_RE = re.compile(r'<([a-zA-Z][^\s/>]*)((?:"[^"]*"|\'[^\']*\'|[^>"\'])*)>')
_ATTR_RE = re.compile(r'([^\s"\'<>/=]+)(?:\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([^\s"\'>]+)))?')
_OPAQUE_RE = re.compile(r'<!--.*?-->|(<(script|style)\b[^>]*>).*?</\2\s*>', re.DOTALL | re.IGNORECASE)
_EXTERNAL_PREFIXES = ("http://", "https://", "mailto:", "javascript:", "data:", "#", "//")


def collect_refs(text: str) -> tuple[str, list[str]]:
    """Return the page's <base href> and every href/src value, in document order."""
    base = "."
    refs: list[str] = []
    for tag in _TAG_RE.finditer(_OPAQUE_RE.sub(lambda m: m.group(1) or "", text)):
        attrs: dict[str, str | None] = {}
        for attr in _ATTR_RE.finditer(tag.group(2)):
            value = next((g for g in attr.groups()[1:] if g is not None), None)
            attrs.setdefault(
                attr.group(1).lower(),
                html.unescape(value) if value is not None else None,
            )
        if tag.group(1).lower() == "base" and attrs.get("href"):
            base = attrs["href"]
            continue
        for name in ("href", "src"):
            if attrs.get(name):
                refs.append(attrs[name])
    return base, refs


def validate_site(repo: Path) -> list[str]:
    errors: list[str] = []

    for page in repo.rglob("*.html"):
        text = page.read_text(encoding="utf-8", errors="replace")
        base, refs = collect_refs(text)

        if "/writeups/" in text or "./writeups/" in text:
            errors.append(f"{page.relative_to(repo)}: stale /writeups/ path")

        page_dir = page.parent.relative_to(repo).as_posix()
        base_dir = posixpath.normpath(posixpath.join(page_dir, base))

        for ref in refs:
            if ref.startswith(_EXTERNAL_PREFIXES):
                continue

            rel = urllib.parse.unquote(ref.split("#", 1)[0].split("?", 1)[0])
            if not rel:
                continue

            target = posixpath.normpath(posixpath.join(base_dir, rel))
            if not (repo / target).exists():
                errors.append(
                    f"{page.relative_to(repo)}: {ref!r} -> missing {target!r}"
                )

    return errors


@dataclass
class BuildResult:
    """Everything a caller (CLI or GUI) needs to know about one build."""

    pages: list[str] = field(default_factory=list)
    preserved_archives: int = 0
    restored_archives: int = 0
    errors: list[str] = field(default_factory=list)
    staged_site: Path | None = None
    changed_files: list[str] = field(default_factory=list)
    export_root: Path | None = None
    keep_staging: bool = False


def git_available(repo: Path) -> bool:
    return (
        (repo / ".git").exists()
        and shutil.which("git") is not None
        and subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
        ).returncode
        == 0
    )


def git_status_lines(repo: Path) -> list[str]:
    """Porcelain status lines (empty when the tree is clean)."""
    proc = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        return []
    return [line for line in proc.stdout.splitlines() if line.strip()]


def git_head(repo: Path) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.stdout.strip() if proc.returncode == 0 else ""


def git_commit_and_push(repo: Path, message: str) -> tuple[bool, str]:
    """
    Stage everything, commit with `message`, and push the current branch.

    Returns (ok, output); output is the combined git output for the GUI log.
    """
    if not git_available(repo):
        return False, "Not a git repository, or git is not installed."
    if not message.strip():
        return False, "Commit message is empty."

    transcript: list[str] = []
    for command in (
        ["git", "-C", str(repo), "add", "-A"],
        ["git", "-C", str(repo), "commit", "-m", message],
        ["git", "-C", str(repo), "push"],
    ):
        proc = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
        transcript.append(f"$ {' '.join(command[3:]) or 'git'}")
        transcript.append((proc.stdout + proc.stderr).strip())
        if proc.returncode != 0:
            return False, "\n".join(line for line in transcript if line)
    return True, "\n".join(line for line in transcript if line)


def build_site(
    repo: Path,
    export: Path,
    *,
    check: bool = False,
    keep_staging: bool = False,
    config: dict | None = None,
    log=print,
) -> BuildResult:
    """
    Merge an Obsidian export into the repository and validate the result.

    The whole site is assembled in a staging copy first; the live repository
    files are only replaced after validation passes. With check=True nothing
    in the repository is written. With keep_staging=True the repository is
    also left untouched and the staging tree stays on disk for previewing;
    the caller owns its cleanup.
    """
    repo = repo.resolve()
    export = export.resolve()

    if not (repo / "index.html").exists():
        die(f"{repo} is not the repo root: index.html is missing.")
    if not (repo / "site.css").exists():
        die(f"{repo}/site.css is missing.")

    if config is None:
        config = load_config(repo)

    result = BuildResult(keep_staging=keep_staging)

    if keep_staging:
        temp_root = Path(tempfile.mkdtemp(prefix="ctf-export-"))
        temp_ctx = None
    else:
        temp_ctx = tempfile.TemporaryDirectory(prefix="ctf-export-")
        temp_root = Path(temp_ctx.name)

    try:
        export_root = find_export_root(export, temp_root / "export")
        if export_root.is_relative_to(repo) or repo.is_relative_to(export_root):
            die("Export and repository directories must be separate and must not contain one another.")
        result.export_root = export_root
        log(f"[*] Export root: {export_root}")

        metadata = read_metadata(export_root)
        roots = generated_roots(repo, export_root)
        checked_generated_targets(repo, roots)
        verify_fonts(repo)

        # Build and validate a complete candidate before changing the live files.
        staged = temp_root / "site"
        shutil.copytree(repo, staged, ignore=shutil.ignore_patterns(".git", "__pycache__"))
        custom_assets = {
            name: (staged / name).read_bytes()
            for name in PERSISTENT_GENERATED_FILES if (staged / name).is_file()
        }
        archive_stash, preserved_archives = stash_manual_archives(staged, temp_root, roots)

        clear_generated_export(staged, export_root)
        overlay_export(export_root, staged)

        restored_archives = restore_manual_archives(
            staged,
            archive_stash,
            preserved_archives,
        )
        for name, data in custom_assets.items():
            asset = staged / name
            asset.parent.mkdir(parents=True, exist_ok=True)
            asset.write_bytes(data)

        clean_exported_html(staged)

        metadata = read_metadata(staged)
        update_index(staged, metadata, config)
        ensure_wide_writeups(staged)

        verify_fonts(staged)

        result.errors = validate_site(staged)
        if result.errors:
            return result

        result.pages = [
            rec["exportPath"]
            for rec in webpage_records(metadata)
            if rec.get("exportPath") and "/media/" not in rec["exportPath"]
        ]
        result.preserved_archives = len(preserved_archives)
        result.restored_archives = restored_archives

        if check or keep_staging:
            # Preview/check modes never touch the repository; keep the staged
            # tree around only when the caller asked to serve it.
            result.staged_site = staged if keep_staging else None
            return result

        # Only generated output and the refreshed landing page/style get written.
        clear_generated_export(repo, export_root)
        for name in sorted(roots):
            source, dest = staged / name, repo / name
            if source.is_dir():
                shutil.copytree(source, dest)
            elif source.is_file():
                shutil.copy2(source, dest)
            result.changed_files.append(name)
        for name in ("index.html", "site.css"):
            if (repo / name).read_bytes() != (staged / name).read_bytes():
                shutil.copy2(staged / name, repo / name)
                result.changed_files.append(name)

        result.staged_site = staged
        return result
    finally:
        if temp_ctx is not None:
            temp_ctx.cleanup()


def print_summary_via(write, result: BuildResult) -> None:
    write(f"[+] Export merged: {len(result.pages)} markdown pages discovered.")
    write("[+] Landing page regenerated from export metadata.")
    write("[+] Pages rebuilt as plain HTML; Obsidian CSS/JS shell removed.")
    write("[+] site.css is the only stylesheet on every page.")
    write("[+] Homepage styling, self-hosted fonts, and custom favicon preserved.")
    write("[+] Wide writeup layout + centered images: OK")
    if result.preserved_archives:
        write(
            f"[+] Archive attachments preserved: {result.preserved_archives} "
            f"checked, {result.restored_archives} restored."
        )
    write("[+] Home links normalized to publish-root index.html: OK")
    write("[+] Archive download links bypass Obsidian routing: OK")
    write("[+] Local href/src validation: OK")
    write("[+] No git commit or push was performed.")


def print_summary(result: BuildResult) -> None:
    print_summary_via(print, result)


def print_errors(errors: list[str]) -> None:
    print("[!] Validation failed; repository files were not changed:")
    for error in errors[:50]:
        print(f"    {error}")
    if len(errors) > 50:
        print(f"    ... and {len(errors) - 50} more")


def page_entries(export_root: Path, config: dict) -> list[dict]:
    """Per-page data for the GUI: the values in effect, plus the defaults they can revert to."""
    records = webpage_records(read_metadata(export_root))
    entries = []
    for rec in records:
        path = rec.get("exportPath", "")
        if not path or "/media/" in path:
            continue

        top = path.split("/", 1)[0] if "/" in path else ""
        event = config["events"].get(top, {})
        event_name = event.get("name") or (top.replace("-", " ").title() if top else "Site")

        fallback = {
            "title": fallback_title(rec),
            "description": fallback_description(rec, event_name),
            "tags": list(event.get("default_tags", [])),
        }
        override = config["landing_overrides"].get(path)
        entries.append({
            "path": path,
            "event": top,
            "fallback": fallback,
            "override": deepcopy(override) if override else None,
        })
    return entries


GUI_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Publish Control Panel</title>
<style>
  :root {
    --bg: #d0b36f; --panel: #faf4e6; --ink: #1b1712; --soft: #6b6052;
    --accent: #d12a1e; --ok: #3f7d3a; --code-bg: #211c15; --code-ink: #f0e7d4;
    --mono: "IBM Plex Mono", ui-monospace, monospace;
    --body: "IBM Plex Sans", system-ui, sans-serif;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--ink);
    font: 15px/1.5 var(--body);
  }
  header {
    padding: 1.2rem 1.5rem; border-bottom: 3px solid var(--ink);
    background: repeating-linear-gradient(45deg, transparent 0 14px, rgba(27,23,18,.04) 14px 15px);
  }
  header h1 { margin: 0; font: 1.4rem var(--mono); letter-spacing: .02em; }
  header h1 .caret { color: var(--accent); }
  header p { margin: .3rem 0 0; color: var(--soft); font-size: .85rem; }
  main { max-width: 76rem; margin: 0 auto; padding: 1.2rem 1.5rem 4rem; }
  section {
    background: var(--panel); border: 2px solid var(--ink);
    box-shadow: 5px 5px 0 rgba(27,23,18,.35);
    margin-bottom: 1.4rem; padding: 1rem 1.2rem;
  }
  section h2 {
    margin: 0 0 .8rem; font: 1rem var(--mono);
    border-bottom: 1px dashed var(--soft); padding-bottom: .4rem;
  }
  section h2::before { content: "> "; color: var(--accent); }
  .row { display: flex; gap: .6rem; align-items: center; flex-wrap: wrap; }
  input[type=text] {
    font: .9rem var(--mono); padding: .4rem .55rem;
    border: 1px solid var(--ink); background: #fff; color: var(--ink);
  }
  input[type=text]:focus { outline: 2px solid var(--accent); outline-offset: 1px; }
  button {
    font: .85rem var(--mono); padding: .45rem .9rem; cursor: pointer;
    border: 2px solid var(--ink); background: var(--panel); color: var(--ink);
    box-shadow: 3px 3px 0 rgba(27,23,18,.45); transition: transform .05s;
  }
  button:hover:not(:disabled) { background: var(--bg); }
  button:active:not(:disabled) { transform: translate(2px, 2px); box-shadow: 1px 1px 0 rgba(27,23,18,.45); }
  button:disabled { opacity: .45; cursor: not-allowed; }
  button.primary { background: var(--accent); color: #fff; border-color: var(--ink); }
  button.primary:hover:not(:disabled) { background: #b0231a; }
  .muted { color: var(--soft); font-size: .85rem; }
  .error { color: var(--accent); font-size: .85rem; }
  table { border-collapse: collapse; width: 100%; font-size: .88rem; }
  th, td { text-align: left; padding: .35rem .5rem; border-bottom: 1px solid rgba(27,23,18,.2); vertical-align: top; }
  th { font: .78rem var(--mono); text-transform: uppercase; letter-spacing: .05em; color: var(--soft); }
  td.path { font: .8rem var(--mono); white-space: nowrap; }
  td input { width: 100%; }
  .tag-note { font-size: .72rem; color: var(--soft); }
  .badge {
    display: inline-block; font: .7rem var(--mono); padding: .1rem .4rem;
    border: 1px solid var(--accent); color: var(--accent); border-radius: 2px;
  }
  a.preview-link { color: var(--accent); font: .85rem var(--mono); }
  pre#log {
    background: var(--code-bg); color: var(--code-ink); font: .8rem/1.45 var(--mono);
    padding: .8rem 1rem; margin: 0; max-height: 20rem; overflow: auto;
    white-space: pre-wrap; word-break: break-word; min-height: 3.2rem;
  }
  ul#errors { margin: .5rem 0 0; padding-left: 1.2rem; }
  ul#errors li { color: var(--accent); font: .82rem var(--mono); margin: .15rem 0; }
  .status-pill {
    font: .75rem var(--mono); padding: .15rem .55rem; border: 1px solid var(--ink);
    background: #fff; margin-left: .6rem;
  }
  .status-pill.running { background: #f0c674; }
  .status-pill.done { background: #a9d89a; }
  .status-pill.failed { background: var(--accent); color: #fff; }
  .event-block { margin-bottom: 1rem; }
  .event-block h3 { margin: .6rem 0 .3rem; font: .9rem var(--mono); }
  .event-block h3::before { content: "./"; color: var(--accent); }
  #js-watchdog, #js-noscript, #js-neterr {
    display: none; background: #d12a1e; color: #fff; border: 2px solid var(--ink);
    padding: .8rem 1rem; margin-bottom: 1.2rem; font: .9rem/1.5 var(--mono);
    box-shadow: 5px 5px 0 rgba(27,23,18,.35);
  }
  #js-noscript { display: block; }
  #js-watchdog b, #js-noscript b { color: #ffe08a; }
</style>
</head>
<body>
<noscript>
  <div style="display:block; background:#d12a1e; color:#fff; border:2px solid #1b1712; padding:.8rem 1rem; margin:1rem auto; max-width:72rem; font-family:monospace;">
    JavaScript is disabled or blocked. This dashboard needs it to work.
  </div>
</noscript>
<script>
/* Watchdog (ES5-only, independent of the main script). It reports the exact
   reason the dashboard is dead, so no DevTools are needed:
     - fewer than 2 <script> blocks  -> the HTML was truncated in transit
     - a captured JS error           -> the script ran but threw
     - otherwise                     -> the script block is present but blocked */
window.appReady = false;
window.__jsErrors = [];
window.addEventListener("error", function (ev) {
  window.__jsErrors.push(String(ev.message || (ev.error && ev.error.message) || "unknown error") +
    (ev.lineno ? (" (line " + ev.lineno + ")") : ""));
});
window.addEventListener("load", function () {
  setTimeout(function () {
    if (window.appReady) return;
    var banner = document.getElementById("js-watchdog");
    if (!banner) return;
    var blocks = document.querySelectorAll("script").length;
    var msg;
    if (blocks < 2) {
      msg = "HTML TRUNCATED IN TRANSIT: only " + blocks + " script block(s) arrived, the " +
            "dashboard script at the end of the page was cut off. Reload the page; if it " +
            "persists, try http://127.0.0.1:" + location.port + "/ instead.";
    } else if (window.__jsErrors.length) {
      msg = "SCRIPT ERROR: " + window.__jsErrors.join(" | ");
    } else {
      msg = "The dashboard script block arrived but did not run and raised no error — " +
            "it is being blocked (browser setting, private mode, or an extension).";
    }
    banner.textContent = "⚠ " + msg;
    banner.style.display = "block";
  }, 500);
});
</script>
<header>
  <div id="js-watchdog">
    &#9888; The dashboard's JavaScript did not run &#8212; the buttons above are dead.
    Either this browser blocked the script (try another browser or disable
    extensions / private-mode protection), or it is too old to understand it.
    Details are in the server console and in <code>.publish-gui.log</code>.
  </div>
  <div id="js-neterr">
    &#9888; The page loaded but cannot reach the local server &#8212; every button will
    appear dead. This is a WSL/networking issue, not a scripting one. Use the URL
    printed in the terminal (the <code>http://&lt;ip&gt;:&lt;port&gt;/</code> one), not
    <code>127.0.0.1</code>. See <code>.publish-gui.log</code>.
  </div>
  <h1><span class="caret">&gt;</span> publish control panel</h1>
  <p>prepare_publish.py --gui &mdash; local only, nothing is published until you push.</p>
</header>
<main>

<section>
  <h2>export source</h2>
  <div class="row">
    <input type="text" id="export-path" style="flex:1; min-width:20rem" spellcheck="false">
    <button id="btn-test" onclick="testExport()">Test export</button>
    <span id="export-status" class="muted"></span>
  </div>
</section>

<section>
  <h2>actions</h2>
  <div class="row">
    <button id="btn-check" onclick="build('check')">Check</button>
    <button id="btn-preview" onclick="build('preview')">Preview</button>
    <button id="btn-publish" class="primary" onclick="build('publish')">Publish</button>
    <a id="preview-link" class="preview-link" href="/preview/" target="_blank" hidden>&#x2197; open preview</a>
    <span id="job-pill" class="status-pill">idle</span>
  </div>
  <p class="muted" style="margin:.6rem 0 0">
    Check validates without touching the repo. Preview builds the candidate site and serves it
    locally. Publish writes the result into the working tree (still no git commit).
  </p>
</section>

<section>
  <h2>writeups <span class="muted" style="text-transform:none">(edits are saved to publish_config.json)</span></h2>
  <div id="pages">Loading&hellip;</div>
  <div class="row" style="margin-top:.7rem">
    <button class="primary" onclick="saveConfig()">Save changes</button>
    <span id="config-status" class="muted"></span>
  </div>
</section>

<section>
  <h2>events</h2>
  <div id="events"></div>
</section>

<section>
  <h2>git</h2>
  <div id="git-info" class="muted"></div>
  <div class="row" style="margin-top:.5rem">
    <input type="text" id="commit-msg" style="flex:1; min-width:20rem" spellcheck="false">
    <button id="btn-commit" onclick="commitPush()">Commit &amp; push</button>
  </div>
  <p class="muted" style="margin:.6rem 0 0">
    Pushing to main triggers the GitHub Actions workflow that deploys to the VPS.
  </p>
</section>

<section>
  <h2>output</h2>
  <pre id="log">Ready.</pre>
  <ul id="errors"></ul>
</section>

</main>
<script>
window.appReady = true;   // proves this script block executed (watchdog checks it)
fetch('/api/ping').catch(function () {
  var b = document.getElementById('js-neterr');
  if (b) { b.style.display = 'block'; }
});

let S = null;          // last /api/state payload
let edits = {};        // path -> override object, or null to reset to defaults
let shownLog = 0;      // log lines already rendered
let polling = null;

const $ = id => document.getElementById(id);
const api = async (path, opts) => {
  const r = await fetch(path, opts);
  const data = await r.json().catch(() => ({}));
  return { ok: r.ok, data };
};
const post = (path, body) => api(path, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body || {}),
});
const esc = s => String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

async function loadState() {
  const { data } = await api('/api/state');
  S = data;
  render();
}

function render() {
  $('export-path').value = S.export;
  const es = $('export-status');
  if (S.export_error) { es.textContent = S.export_error; es.className = 'error'; }
  else { es.textContent = S.pages.length + ' page(s) found'; es.className = 'muted'; }

  renderPages();
  renderEvents();
  renderGit();
  renderJob(S.job);
  $('preview-link').hidden = !S.preview_ready;
}

function effective(page) {
  return (page.path in edits) ? edits[page.path] : page.override;
}

function renderPages() {
  const groups = {};
  for (const page of S.pages) {
    const key = page.event || '';
    if (!groups[key]) groups[key] = [];
    groups[key].push(page);
  }

  let html = '';
  for (const [event, pages] of Object.entries(groups)) {
    html += '<div class="event-block">';
    if (event) html += '<h3>' + esc(event) + '</h3>';
    html += '<table><tr><th>page</th><th>title</th><th>description</th><th>tags</th><th></th></tr>';
    for (const page of pages) {
      const ov = effective(page);
      const base = ov || page.fallback;
      html += '<tr>'
        + '<td class="path">' + esc(page.path) + (ov ? ' <span class="badge">override</span>' : '') + '</td>'
        + '<td><input type="text" value="' + esc(base.title) + '" data-path="' + esc(page.path) + '" data-field="title"></td>'
        + '<td><input type="text" value="' + esc(base.description) + '" data-path="' + esc(page.path) + '" data-field="description"></td>'
        + '<td><input type="text" value="' + esc((base.tags || []).join(', ')) + '" data-path="' + esc(page.path) + '" data-field="tags">'
        + '<div class="tag-note">comma separated</div></td>'
        + '<td>' + (ov ? '<button title="reset to defaults" onclick="resetOverride(&apos;' + esc(page.path) + '&apos;)">&#x21ba;</button>' : '') + '</td>'
        + '</tr>';
    }
    html += '</table></div>';
  }
  $('pages').innerHTML = html || '<p class="muted">No pages discovered yet.</p>';

  $('pages').querySelectorAll('input[data-path]').forEach(input => {
    input.addEventListener('input', () => {
      const path = input.dataset.path;
      const page = S.pages.find(p => p.path === path);
      const current = { ...(effective(page) || page.fallback) };
      if (input.dataset.field === 'tags')
        current.tags = input.value.split(',').map(t => t.trim()).filter(Boolean);
      else
        current[input.dataset.field] = input.value;
      edits[path] = current;
    });
  });
}

function resetOverride(path) { delete edits[path]; renderPages(); }

function renderEvents() {
  const events = S.config.events || {};
  let html = '<table><tr><th>folder</th><th>display name</th><th>note</th><th>default tags</th></tr>';
  for (const [key, ev] of Object.entries(events)) {
    html += '<tr>'
      + '<td class="path">' + esc(key) + '</td>'
      + '<td><input type="text" value="' + esc(ev.name) + '" data-event="' + esc(key) + '" data-field="name"></td>'
      + '<td><input type="text" value="' + esc(ev.note) + '" data-event="' + esc(key) + '" data-field="note"></td>'
      + '<td><input type="text" value="' + esc((ev.default_tags || []).join(', ')) + '" data-event="' + esc(key) + '" data-field="default_tags"></td>'
      + '</tr>';
  }
  html += '</table>';
  $('events').innerHTML = html;

  $('events').querySelectorAll('input[data-event]').forEach(input => {
    input.addEventListener('input', () => {
      const ev = S.config.events[input.dataset.event];
      if (input.dataset.field === 'default_tags')
        ev.default_tags = input.value.split(',').map(t => t.trim()).filter(Boolean);
      else
        ev[input.dataset.field] = input.value;
    });
  });
}

function renderGit() {
  const g = S.git;
  const el = $('git-info');
  if (!g.available) { el.textContent = 'Git is not available in this repository.'; $('btn-commit').disabled = true; return; }
  el.innerHTML = 'branch <b>' + esc(g.branch) + '</b> &mdash; '
    + (g.dirty.length
        ? '<span class="error">' + g.dirty.length + ' uncommitted change(s)</span><br>' + g.dirty.map(esc).join('<br>')
        : 'working tree clean');
  $('btn-commit').disabled = g.dirty.length === 0;
}

function renderJob(job) {
  const pill = $('job-pill');
  pill.textContent = job.state + (job.mode && job.state === 'running' ? ' (' + job.mode + ')' : '');
  pill.className = 'status-pill ' + job.state;

  const busy = job.state === 'running';
  for (const id of ['btn-check', 'btn-preview', 'btn-publish', 'btn-test']) $(id).disabled = busy;

  if (job.log.length !== shownLog) {
    const extra = job.log.slice(shownLog).join('\\n');
    $('log').textContent += ($('log').textContent === 'Ready.' ? '' : '\\n') + extra;
    shownLog = job.log.length;
    $('log').scrollTop = $('log').scrollHeight;
  }

  const list = $('errors');
  list.innerHTML = job.errors.map(e => {
    const page = e.split(':')[0];
    const link = page.endsWith('.html') ? ' <a href="/preview/' + encodeURIComponent(page) + '" target="_blank">open</a>' : '';
    return '<li>' + esc(e) + link + '</li>';
  }).join('');
}

function startPolling() {
  if (polling) return;
  polling = setInterval(async () => {
    const { data } = await api('/api/status');
    const wasRunning = polling !== null;
    renderJob(data);
    if (data.state !== 'running') {
      clearInterval(polling); polling = null;
      loadState();
    }
  }, 600);
}

async function testExport() {
  const status = $('export-status');
  status.textContent = 'Testing&hellip;'; status.className = 'muted';
  const { ok, data } = await post('/api/export', { path: $('export-path').value });
  if (ok) { status.textContent = 'Looks good: ' + data.root; status.className = 'muted'; loadState(); }
  else { status.textContent = data.error || 'Failed.'; status.className = 'error'; }
}

async function build(mode) {
  if (mode === 'publish' && !confirm('Publish into the working tree? Nothing is committed until you use Commit & push.'))
    return;
  $('log').textContent = '';
  shownLog = 0;
  $('errors').innerHTML = '';
  const { ok, data } = await post('/api/build', { mode });
  if (!ok) { $('log').textContent = data.error || 'Failed to start.'; return; }
  renderJob({ state: 'running', mode, log: [], errors: [] });
  startPolling();
  if (mode === 'preview') setTimeout(() => window.open('/preview/', '_blank'), 800);
}

async function saveConfig() {
  const status = $('config-status');
  const config = JSON.parse(JSON.stringify(S.config));
  for (const page of S.pages) {
    if (!(page.path in edits)) continue;
    if (edits[page.path] === null) delete config.landing_overrides[page.path];
    else config.landing_overrides[page.path] = edits[page.path];
  }
  const { ok, data } = await post('/api/config', config);
  if (ok) {
    edits = {};
    status.textContent = 'Saved to ' + data.path;
    status.className = 'muted';
    loadState();
  } else {
    status.textContent = data.error || 'Save failed.';
    status.className = 'error';
  }
}

async function commitPush() {
  const btn = $('btn-commit');
  btn.disabled = true;
  $('log').textContent = '$ git add -A && git commit && git push\\n';
  shownLog = 0;
  const { ok, data } = await post('/api/git', { message: $('commit-msg').value });
  $('log').textContent += (data.output || (data.error || 'Failed.')) + (ok ? '\\n[+] Pushed.' : '');
  loadState();
}

window.addEventListener('load', async () => {
  await loadState();
  $('commit-msg').value = 'Publish site update ' + new Date().toISOString().slice(0, 16).replace('T', ' ');
  if (S.job.state === 'running') startPolling();
});
</script>
</body>
</html>
"""


class BuildJob:
    """One background build at a time, with a log the GUI can poll."""

    def __init__(self, repo: Path, logger: logging.Logger | None = None) -> None:
        self.repo = repo
        self.logger = logger
        self.lock = threading.Lock()
        self.state = "idle"      # idle | running | done | failed
        self.mode = ""
        self.log: list[str] = []
        self.errors: list[str] = []
        self.summary: dict = {}
        self.thread: threading.Thread | None = None

    def is_running(self) -> bool:
        return self.state == "running"

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "state": self.state,
                "mode": self.mode,
                "log": list(self.log),
                "errors": list(self.errors),
                "summary": dict(self.summary),
            }

    def _write(self, text: str) -> None:
        with self.lock:
            self.log.extend(text.splitlines() or [text])

    def start(self, mode: str, work) -> bool:
        with self.lock:
            if self.state == "running":
                if self.logger:
                    self.logger.info("build %r rejected: another build is still running", mode)
                return False
            self.state = "running"
            self.mode = mode
            self.log = []
            self.errors = []
            self.summary = {}

        started = time.monotonic()
        if self.logger:
            self.logger.info("build %r started", mode)

        def runner() -> None:
            try:
                work(self._write)
            except SystemExit as error:
                with self.lock:
                    self.state = "failed"
                    self.errors = [str(error).lstrip("[!] ")]
                if self.logger:
                    self.logger.info(
                        "build %r aborted after %.1fs: %s",
                        mode, time.monotonic() - started, self.errors[0],
                    )
            except Exception as error:  # surface anything else instead of dying silently
                with self.lock:
                    self.state = "failed"
                    self.errors = [f"{type(error).__name__}: {error}"]
                if self.logger:
                    self.logger.exception("build %r crashed after %.1fs", mode, time.monotonic() - started)
            else:
                with self.lock:
                    if self.state == "running":
                        self.state = "done"
                if self.logger:
                    self.logger.info(
                        "build %r finished as %s after %.1fs (%d error(s))",
                        mode, self.state, time.monotonic() - started, len(self.errors),
                    )

        self.thread = threading.Thread(target=runner, daemon=True)
        self.thread.start()
        return True


def _detect_wsl_ip() -> str | None:
    """
    Best-effort local (WSL/host) IP for the browser to use.

    A connected UDP socket triggers the routing table to pick an outbound
    interface address without sending any packets. Loopback is skipped.
    """
    import socket as _socket

    for probe_host in ("1.1.1.1", "8.8.8.8"):
        try:
            with _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM) as sock:
                sock.connect((probe_host, 80))
                ip = sock.getsockname()[0]
            if ip and not ip.startswith("127."):
                return ip
        except OSError:
            continue
    return None


def run_gui(
    repo: Path,
    log_file: Path | None = None,
    verbose: bool = False,
) -> None:
    """Local web UI: pick an export, edit titles/tags, preview, publish, commit."""
    try:
        from flask import Flask, Response, jsonify, request, send_from_directory
    except ImportError:
        die(
            "Flask is required for --gui. Install it with:\n"
            "    py -3 -m pip install flask\n"
            "The CLI (without --gui) works without Flask."
        )

    import socket
    import webbrowser

    repo = repo.resolve()
    if not (repo / "index.html").exists() or not (repo / "site.css").exists():
        die(f"{repo} is not the repo root: index.html/site.css is missing.")

    app = Flask(__name__)

    import importlib.metadata
    import sys

    log_file = log_file or (repo / LOG_FILE)
    logger = setup_logging(log_file, verbose=verbose)
    logger.info("=" * 60)
    logger.info(
        "GUI session start | python %s | flask %s | repo %s",
        sys.version.split()[0],
        importlib.metadata.version("flask"),
        repo,
    )
    job = BuildJob(repo, logger)
    state: dict = {"export": str((repo / DEFAULT_EXPORT).resolve())}
    preview: dict = {"root": None}   # directory currently served at /preview/

    @app.before_request
    def log_request():
        # Every browser request lands here; a silent UI shows up as a gap.
        if request.path.startswith("/preview/"):
            logger.debug("GET %s", request.path)   # preview asset spam stays at DEBUG
        else:
            logger.info("%s %s", request.method, request.path)

    def dispose_preview() -> None:
        root = preview.get("root")
        preview["root"] = None
        # The preview root is either the repository itself or a temp staging
        # tree; only the staging tree is ours to delete.
        if root is not None and root != repo:
            logger.debug("removing staging tree %s", root)
            shutil.rmtree(root, ignore_errors=True)

    def probe_export(raw: str) -> Path:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = repo / path
        with tempfile.TemporaryDirectory(prefix="ctf-probe-") as temp:
            return find_export_root(path, Path(temp))

    @app.get("/")
    def index_page() -> Response:
        return Response(GUI_HTML, mimetype="text/html")

    @app.get("/api/ping")
    def api_ping() -> Response:
        # Sent by the dashboard script the moment it starts executing; if this
        # never shows up in the log, the browser never ran the JavaScript.
        logger.info("browser JS is alive (ping received)")
        return jsonify({"ok": True, "pong": True})

    @app.get("/api/state")
    def api_state() -> Response:
        config = load_config(repo)
        export = Path(state["export"])
        pages: list[dict] = []
        export_error = None
        try:
            export_root = probe_export(str(export))
            pages = page_entries(export_root, config)
        except SystemExit as error:
            export_error = str(error).lstrip("[!] ")

        return jsonify({
            "export": str(export),
            "export_error": export_error,
            "pages": pages,
            "config": config,
            "job": job.snapshot(),
            "preview_ready": preview.get("root") is not None,
            "git": {
                "available": git_available(repo),
                "branch": git_head(repo),
                "dirty": git_status_lines(repo),
            },
        })

    @app.post("/api/export")
    def api_export() -> Response:
        raw = (request.get_json(silent=True) or {}).get("path", "").strip()
        if not raw:
            return jsonify({"ok": False, "error": "No path given."})
        try:
            export_root = probe_export(raw)
        except SystemExit as error:
            logger.warning("export probe FAILED for %r: %s", raw, error)
            return jsonify({"ok": False, "error": str(error).lstrip("[!] ")})
        logger.info("export probe ok: %r -> %s", raw, export_root)
        state["export"] = raw
        return jsonify({"ok": True, "root": str(export_root)})

    @app.post("/api/config")
    def api_config() -> Response:
        config = request.get_json(silent=True)
        try:
            validate_config(config)
        except SystemExit as error:
            logger.warning("config save rejected: %s", error)
            return jsonify({"ok": False, "error": str(error).lstrip("[!] ")})
        save_config(repo, config)
        logger.info(
            "config saved: %d event(s), %d override(s)",
            len(config["events"]), len(config["landing_overrides"]),
        )
        return jsonify({"ok": True, "path": CONFIG_FILE})

    @app.post("/api/build")
    def api_build() -> Response:
        mode = (request.get_json(silent=True) or {}).get("mode", "check")
        if mode not in {"check", "preview", "publish"}:
            return jsonify({"ok": False, "error": f"Unknown mode {mode!r}."}), 400

        export = Path(state["export"]).expanduser()
        if not export.is_absolute():
            export = repo / export
        keep_staging = mode in {"check", "preview"}
        logger.info("build %r requested | export=%s | keep_staging=%s", mode, export, keep_staging)

        def work(write) -> None:
            if keep_staging:
                dispose_preview()
            result = build_site(
                repo,
                export,
                keep_staging=keep_staging,
                log=write,
            )
            if result.errors:
                print_errors_via(write, result.errors)
                logger.warning("validation failed with %d error(s): %s", len(result.errors), result.errors[0])
                with job.lock:
                    job.errors = list(result.errors)
                    job.state = "failed"
                return

            logger.info(
                "build result: %d page(s), changed=%s",
                len(result.pages), result.changed_files or "nothing",
            )

            if mode == "check":
                write("[+] Check passed; repository files were not changed.")
            else:
                print_summary_via(write, result)

            if keep_staging:
                preview["root"] = result.staged_site
            else:
                dispose_preview()
                preview["root"] = repo

            with job.lock:
                job.summary = {
                    "pages": len(result.pages),
                    "changed": list(result.changed_files),
                    "mode": mode,
                }

        if not job.start(mode, work):
            return jsonify({"ok": False, "error": "A build is already running."}), 409
        return jsonify({"ok": True})

    @app.get("/api/status")
    def api_status() -> Response:
        return jsonify(job.snapshot())

    @app.get("/preview/")
    @app.get("/preview/<path:page>")
    def preview_route(page: str = "index.html") -> Response:
        root = preview.get("root")
        if root is None:
            return Response("No preview built yet. Press Preview first.", mimetype="text/plain", status=404)
        # Never serve repository internals (.git etc.).
        if any(part.startswith(".") for part in Path(page).parts):
            return Response("Not found.", mimetype="text/plain", status=404)
        target = root / page
        if target.is_dir():
            target = target / "index.html"
        if not target.is_file():
            return Response("Not found.", mimetype="text/plain", status=404)
        return send_from_directory(root, page if not (root / page).is_dir() else f"{page}/index.html")

    @app.post("/api/git")
    def api_git() -> Response:
        message = (request.get_json(silent=True) or {}).get("message", "")
        if job.is_running():
            logger.info("git commit rejected: a build is still running")
            return jsonify({"ok": False, "error": "A build is running; wait for it to finish."}), 409
        logger.info("git commit+push requested with message %r", message)
        ok, output = git_commit_and_push(repo, message)
        logger.info("git commit+push %s", "succeeded" if ok else "FAILED")
        return jsonify({"ok": ok, "output": output})

    def shutdown(*_) -> None:
        dispose_preview()
        raise SystemExit(0)

    # WSL2's localhost forwarding (127.0.0.1) is unreliable for the browser's
    # pooled fetch() connections: the page loads, then every API call silently
    # dies. Binding all interfaces lets Windows reach us either via the WSL IP
    # (reliable) or localhost, so open the WSL IP when we can detect it.
    host_ip = _detect_wsl_ip()

    with socket.socket() as probe:
        probe.bind(("0.0.0.0", 0))
        port = probe.getsockname()[1]
    url = f"http://{host_ip or '127.0.0.1'}:{port}/"
    print(f"[*] Publish GUI on {url}  (Ctrl+C to stop)")
    print(f"[*] (also reachable at http://127.0.0.1:{port}/ )")
    print(f"[*] Session log: {log_file}")
    logger.info("listening on %s (bound 0.0.0.0:%d, wsl_ip=%s)", url, port, host_ip)
    threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        app.run(host="0.0.0.0", port=port, debug=False, threaded=True, use_reloader=False)
    except KeyboardInterrupt:
        print("\n[*] Stopped.")
    finally:
        logger.info("GUI session end")
        dispose_preview()
        for handler in logger.handlers[:]:
            handler.close()
            logger.removeHandler(handler)


def print_errors_via(write, errors: list[str]) -> None:
    write("[!] Validation failed; repository files were not changed:")
    for error in errors[:50]:
        write(f"    {error}")
    if len(errors) > 50:
        write(f"    ... and {len(errors) - 50} more")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "export",
        nargs="?",
        type=Path,
        default=None,
        help=f"export directory or .zip (default: {DEFAULT_EXPORT} relative to the repo)",
    )
    parser.add_argument(
        "--repo",
        type=Path,
        default=Path(__file__).resolve().parent,
        help="repo root to update (default: this script's directory)",
    )
    parser.add_argument("--check", action="store_true", help="validate the merged export without changing repository files")
    parser.add_argument("--gui", action="store_true", help="start the local web UI (requires Flask)")
    parser.add_argument(
        "--log-file",
        type=Path,
        default=None,
        help=f"GUI session log location (default: {LOG_FILE} in the repo root)",
    )
    parser.add_argument("--verbose", action="store_true", help="log DEBUG details (including preview requests)")
    args = parser.parse_args()

    repo = args.repo.expanduser().resolve()

    if args.gui:
        run_gui(repo, log_file=args.log_file, verbose=args.verbose)
        return

    export_arg = (args.export.expanduser() if args.export else repo / DEFAULT_EXPORT).resolve()

    result = build_site(repo, export_arg, check=args.check)
    if result.errors:
        print_errors(result.errors)
        raise SystemExit(1)
    if args.check:
        print("[+] Check passed; repository files were not changed.")
        return
    print_summary(result)


if __name__ == "__main__":
    main()

