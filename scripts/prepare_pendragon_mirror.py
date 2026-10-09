#!/usr/bin/env python3
"""Post-process an HTTrack mirror: localize public image URLs embedded in Next.js data."""
from __future__ import annotations
import html
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

root = Path(sys.argv[1]).resolve()
if not root.exists():
    raise SystemExit(f"Mirror directory does not exist: {root}")

image_extensions = {".jpg", ".jpeg", ".png", ".webp", ".avif", ".gif", ".svg"}
asset_hosts = {"www.datocms-assets.com", "datocms-assets.com", "image.mux.com"}
url_re = re.compile(r'https?://[^"\'\s<>\\\\]+')
pages = list(root.rglob("*.html"))
urls: set[str] = set()
for page in pages:
    try:
        text = page.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        continue
    for raw in url_re.findall(text):
        url = html.unescape(raw).replace(r"\u0026", "&").rstrip("),;]} ")
        parsed = urllib.parse.urlsplit(url)
        if parsed.hostname not in asset_hosts:
            continue
        if Path(urllib.parse.unquote(parsed.path)).suffix.lower() not in image_extensions:
            continue
        urls.add(urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, "")))

downloaded: dict[str, Path] = {}
failed: list[str] = []
for i, url in enumerate(sorted(urls), 1):
    parsed = urllib.parse.urlsplit(url)
    rel = Path(parsed.netloc) / Path(urllib.parse.unquote(parsed.path).lstrip("/"))
    destination = root / rel
    if destination.exists() and destination.stat().st_size > 0:
        downloaded[url] = destination
        continue
    destination.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; PublicSiteArchive/1.0)"})
    try:
        with urllib.request.urlopen(req, timeout=35) as response, destination.open("wb") as out:
            out.write(response.read())
        if destination.stat().st_size == 0:
            destination.unlink(missing_ok=True)
            raise OSError("empty response")
        downloaded[url] = destination
    except Exception as exc:
        destination.unlink(missing_ok=True)
        failed.append(f"{url} [{type(exc).__name__}: {exc}]")
    time.sleep(0.08)

# Rewrite only image URLs embedded in HTML/Next.js page data, leaving external video
# and streaming URLs untouched. Relative links work when the mirror is served at its root.
rewritten = 0
for page in pages:
    try:
        content = page.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        continue

    def localize(match: re.Match[str]) -> str:
        nonlocal_dummy = None
        raw = match.group(0)
        candidate = html.unescape(raw).replace(r"\u0026", "&").rstrip("),;]} ")
        parsed = urllib.parse.urlsplit(candidate)
        if parsed.hostname not in asset_hosts:
            return raw
        if Path(urllib.parse.unquote(parsed.path)).suffix.lower() not in image_extensions:
            return raw
        key = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))
        local = downloaded.get(key)
        if local is None:
            return raw
        rel = os.path.relpath(local, page.parent).replace(os.sep, "/")
        return rel

    new_content = url_re.sub(localize, content)
    if new_content != content:
        rewritten += 1
        page.write_text(new_content, encoding="utf-8")

# Fetch Next.js SSG data payloads so client-side route changes can work offline too.
build_id = None
for page in pages:
    try:
        m = re.search(r'"buildId"\s*:\s*"([^"]+)"', page.read_text(encoding="utf-8", errors="ignore"))
        if m:
            build_id = m.group(1)
            break
    except OSError:
        pass
json_downloads = 0
json_failures: list[str] = []
if build_id:
    for route in ("index", "story", "map", "episodes", "characters", "lineages"):
        url = f"https://pendragoncycle.com/_next/data/{build_id}/{route}.json"
        dest = root / "pendragoncycle.com" / "_next" / "data" / build_id / f"{route}.json"
        # If HTTrack produced a different domain folder, retain a root-domain copy too.
        if not (root / "pendragoncycle.com").exists():
            dest = root / "_next" / "data" / build_id / f"{route}.json"
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; PublicSiteArchive/1.0)", "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=25) as response:
                data = response.read()
            if data and (b'"pageProps"' in data or b'notFound' in data or b'__N_SSG' in data):
                dest.write_bytes(data)
                json_downloads += 1
            else:
                json_failures.append(f"{url} [unexpected/empty payload]")
        except Exception as exc:
            json_failures.append(f"{url} [{type(exc).__name__}: {exc}]")

# HTTrack normally creates a top-level project index. Add a friendly entry point.
site_index = root / "pendragoncycle.com" / "index.html"
if site_index.exists():
    wrapper = root / "index.html"
    wrapper.write_text(
        '<!doctype html><html lang="en"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta http-equiv="refresh" content="0;url=./pendragoncycle.com/index.html">'
        '<title>Pendragon Cycle mirror</title>'
        '<p>Opening the saved site… <a href="./pendragoncycle.com/index.html">Open Pendragon Cycle</a></p></html>',
        encoding="utf-8"
    )

file_count = sum(1 for p in root.rglob("*") if p.is_file())
report = [
    "Pendragon Cycle public-site mirror report",
    f"HTML pages found: {len(pages)}",
    f"Public image URLs discovered in page data: {len(urls)}",
    f"Public images downloaded/localized: {len(downloaded)}",
    f"HTML pages updated with local image paths: {rewritten}",
    f"Next.js route JSON payloads saved: {json_downloads}",
    f"Files in mirror before report: {file_count}",
    "",
    "Intentionally not downloaded: MP4/M3U8 video streams and analytics/tracking.",
    "Video URLs remain remote, so video playback still requires internet access.",
    "This is a static mirror, not the original server source or backend.",
    "",
    "Image download failures:",
    *(failed or ["None"]),
    "",
    "Next.js JSON fetch failures:",
    *(json_failures or ["None"]),
]
(root / "MIRROR-REPORT.txt").write_text("\n".join(report) + "\n", encoding="utf-8")
print("\n".join(report[:9]))
