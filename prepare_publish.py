#!/usr/bin/env python3
"""
prepare_publish.py

Merge an Obsidian Webpage HTML Export into this repo and make it publish-ready.

Run from the repository root:

    python prepare_publish.py
    python prepare_publish.py ../../Export
    python prepare_publish.py ~/Downloads/Export.zip
    python prepare_publish.py --check

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
import posixpath
import re
import shutil
import tempfile
import urllib.parse
import zipfile
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
    "tests",
}

PERSISTENT_GENERATED_FILES = ("site-lib/media/favicon.png",)
ARCHIVE_SUFFIXES = {".zip", ".7z", ".rar", ".tar", ".tgz", ".gz", ".bz2", ".xz"}


_PERSISTENT_FOLDED = frozenset(entry.casefold() for entry in PERSISTENT_TOP_LEVEL)


def is_persistent(name: str) -> bool:
    return name.casefold() in _PERSISTENT_FOLDED


EVENT_INFO = {
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

LANDING_OVERRIDES = {
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


def die(message: str) -> None:
    raise SystemExit(f"[!] {message}")


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


def build_landing_main(metadata: dict) -> str:
    records = webpage_records(metadata)
    by_path = {rec["exportPath"]: rec for rec in records if rec.get("exportPath")}

    sections = []

    # Resources
    if "tool-setup.html" in by_path:
        ov = LANDING_OVERRIDES["tool-setup.html"]
        sections.append(f"""    <section>
      <h2>&gt; ./resources/</h2>

      <ul>
{card(ov["title"], "tool-setup.html", ov["description"], ov["tags"])}
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
        info = EVENT_INFO.get(top, {})
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

            override = LANDING_OVERRIDES.get(path)
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


def update_index(repo: Path, metadata: dict) -> None:
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
    new_main = build_landing_main(metadata).lstrip()

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
    args = parser.parse_args()

    repo = args.repo.expanduser().resolve()
    export_arg = (args.export.expanduser() if args.export else repo / DEFAULT_EXPORT).resolve()

    if not (repo / "index.html").exists():
        die(f"{repo} is not the repo root: index.html is missing.")
    if not (repo / "site.css").exists():
        die(f"{repo}/site.css is missing.")

    with tempfile.TemporaryDirectory(prefix="ctf-export-") as temp:
        temp_root = Path(temp)
        export_root = find_export_root(export_arg, temp_root / "export")
        if export_root.is_relative_to(repo) or repo.is_relative_to(export_root):
            die("Export and repository directories must be separate and must not contain one another.")
        print(f"[*] Export root: {export_root}")

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
        update_index(staged, metadata)
        ensure_wide_writeups(staged)

        verify_fonts(staged)

        errors = validate_site(staged)
        if errors:
            print("[!] Validation failed; repository files were not changed:")
            for error in errors[:50]:
                print(f"    {error}")
            if len(errors) > 50:
                print(f"    ... and {len(errors) - 50} more")
            raise SystemExit(1)

        if args.check:
            print("[+] Check passed; repository files were not changed.")
            return

        # Only generated output and the refreshed landing page/style get written.
        clear_generated_export(repo, export_root)
        for name in sorted(roots):
            source, dest = staged / name, repo / name
            if source.is_dir():
                shutil.copytree(source, dest)
            elif source.is_file():
                shutil.copy2(source, dest)
        for name in ("index.html", "site.css"):
            if (repo / name).read_bytes() != (staged / name).read_bytes():
                shutil.copy2(staged / name, repo / name)

    discovered = [
        rec["exportPath"]
        for rec in webpage_records(metadata)
        if rec.get("exportPath") and "/media/" not in rec["exportPath"]
    ]

    print(f"[+] Export merged: {len(discovered)} markdown pages discovered.")
    print("[+] Landing page regenerated from export metadata.")
    print("[+] Pages rebuilt as plain HTML; Obsidian CSS/JS shell removed.")
    print("[+] site.css is the only stylesheet on every page.")
    print("[+] Homepage styling, self-hosted fonts, and custom favicon preserved.")
    print("[+] Wide writeup layout + centered images: OK")
    if preserved_archives:
        print(
            f"[+] Archive attachments preserved: {len(preserved_archives)} "
            f"checked, {restored_archives} restored."
        )
    print("[+] Home links normalized to publish-root index.html: OK")
    print("[+] Archive download links bypass Obsidian routing: OK")
    print("[+] Local href/src validation: OK")
    print("[+] No git commit or push was performed.")


if __name__ == "__main__":
    main()

