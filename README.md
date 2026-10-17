# CTF Writeups

This repository contains my Capture The Flag (CTF) writeups.

I try to document the reasoning process, failed hypotheses, artifacts, and tools used along the way because, in my opinion, those are the best parts of a CTF challenge.

### [View the Live Website](https://dfmari.github.io/ctf_writeups/)

---

## Current Writeups

| Challenge / Series | Event | Category | Vulnerabilities / Techniques |
| --- | --- | --- | --- |
| **MSN Revive** | srdnlenCTF 2026 | Web | Missing authorization, object-level access control, path canonicalization / parser differential |
| **Whoami** | BelkaCTF #7 | DFIR | Windows memory forensics, MemProcFS, process / environment artifact analysis |
| **Locker** | BelkaCTF #7 | DFIR / Malware | MemProcFS, `findevil`, suspicious executable memory, process triage |

---

## BelkaCTF #7 Series

I participated in BelkaCTF #7 live and finished **33rd overall / 10th among students**.

The DFIR series revisits the challenges from scratch using freely available tools, with an emphasis on reproducibility and understanding the underlying artifacts rather than simply reproducing the flags.

Tools used include:

- MemProcFS
- Volatility 3
- standard Linux utilities
- additional free tools as they become relevant

---

## Repository

The site is authored primarily in Obsidian and exported to static HTML.

The publish script rebuilds each exported page as plain HTML: the Obsidian CSS/JS shell is stripped and `site.css` is the only stylesheet, so restyling the whole site means editing that one file.

## Refresh the published site

The easiest way is the local web GUI. Install Flask once, then start it:

```bash
py -3 -m pip install flask
py -3 prepare_publish.py --gui
```

The dashboard opens in your browser and lets you:

- point at an Obsidian export (folder or `.zip`) and verify it was detected,
- edit landing-page titles, descriptions, and tags without touching Python,
- **Check** (validate only), **Preview** (browse the candidate site before it goes live),
  and **Publish** into the working tree,
- see validation errors with direct links into the preview,
- commit and push in one step (pushing to `main` deploys to the VPS via GitHub Actions).

For a development update, work on the local `dev` branch, use **Check/Preview** and
then **Publish** to prepare the working tree, and choose **Commit & push to dev**.
This stages all changes and commits on `dev`, then explicitly pushes `HEAD` to
`origin/dev`, regardless of the configured upstream. The button is disabled on other
branches, and the server also checks the branch before staging. It never switches
branches, merges, or force-pushes. The original **Commit & push** retains its existing
current-branch behavior. Neither button builds the export automatically.

The existing deployment workflow listens only to `main`, so a dev push does not
update the live site or create a separate hosted dev site. A broken export still
needs to be corrected before Check/Preview/Publish can succeed.

The GUI reads and writes `publish_config.json` in the repository root; the CLI reads the
same file, so both paths stay in sync. The GUI is meant for local use only and is never
deployed to the server (it binds all interfaces and prints the right URL to open — the
WSL IP under WSL2, otherwise `127.0.0.1`). Every GUI session is recorded in `.publish-gui.log` in the
repository root (`--log-file` to move it, `--verbose` for request-level detail) — useful
when a button seems unresponsive: if the log shows no `ping` after the page loaded, the
browser never ran the dashboard's JavaScript.

The same operations are available from the command line, which needs no third-party
packages. Export the notes with Obsidian Webpage HTML Export into `Writeups/Export`, then
run from this repository:

```bash
python prepare_publish.py --check
python prepare_publish.py
```

The default export is `../../Export` relative to the repository. You can also pass another export directory or a ZIP file:

```bash
python prepare_publish.py ~/Downloads/Export.zip
```

