#!/usr/bin/env python3
"""Post-process an HTTrack mirror: localize public image and short-loop video URLs."""
from __future__ import annotations
import html
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

root = Path(sys.argv[1]).resolve()
if not root.exists():
    raise SystemExit(f"Mirror directory does not exist: {root}")

image_extensions = {".jpg", ".jpeg", ".png", ".webp", ".avif", ".gif", ".svg"}
video_extensions = {".mp4"}
asset_hosts = {"www.datocms-assets.com", "datocms-assets.com"}
# Known short looping background/menu videos present in the public page data.
# The long official trailer and streaming manifests are deliberately excluded.
allowed_video_names = {
    "pendragon-site-home-gif-1.mp4",
    "pendragon-site-story-loop-1.mp4",
    "map1.mp4",
    "pendragon-site-episodes-loop-1.mp4",
    "pendragon-site-character-loop-1.mp4",
    "lineage-v3.mp4",
    "pendragon-subscribe-menu_1280x720.mp4",
}
url_re = re.compile(r'https?://[^"\'\s<>\\\\]+')
pages = list(root.rglob("*.html"))
urls: set[str] = set()
video_urls: set[str] = set()

for page in pages:
    try:
        page_text = page.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        continue
    for raw in url_re.findall(page_text):
        url = html.unescape(raw).replace(r"\u0026", "&").rstrip("),;]} ")
        parsed = urllib.parse.urlsplit(url)
        if parsed.hostname not in asset_hosts:
            continue
        suffix = Path(urllib.parse.unquote(parsed.path)).suffix.lower()
        name = Path(urllib.parse.unquote(parsed.path)).name
        normalized = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))
        if suffix in image_extensions:
            urls.add(normalized)
        elif suffix in video_extensions and any(name.endswith(allowed) for allowed in allowed_video_names):
            video_urls.add(normalized)

downloaded: dict[str, Path] = {}
failed: list[str] = []
all_assets = [(u, "image") for u in sorted(urls)] + [(u, "loop-video") for u in sorted(video_urls)]
for i, (url, kind) in enumerate(all_assets, 1):
    parsed = urllib.parse.urlsplit(url)
    rel = Path(parsed.netloc) / Path(urllib.parse.unquote(parsed.path).lstrip("/"))
    destination = root / rel
    if destination.exists() and destination.stat().st_size > 0:
        downloaded[url] = destination
        continue
    destination.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; PublicSiteArchive/1.0)"})
    try:
        with urllib.request.urlopen(req, timeout=120 if kind == "loop-video" else 35) as response, destination.open("wb") as out:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
        if destination.stat().st_size == 0:
            destination.unlink(missing_ok=True)
            raise OSError("empty response")
        downloaded[url] = destination
    except Exception as exc:
        destination.unlink(missing_ok=True)
        failed.append(f"{url} [{kind}; {type(exc).__name__}: {exc}]")
    time.sleep(0.08)

# Rewrite downloaded image/loop-video URLs within the HTML, including embedded Next.js data.
rewritten = 0
for page in pages:
    try:
        page_text = page.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        continue

    def localize(match: re.Match[str]) -> str:
        raw = match.group(0)
        candidate = html.unescape(raw).replace(r"\u0026", "&").rstrip("),;]} ")
        parsed = urllib.parse.urlsplit(candidate)
        if parsed.hostname not in asset_hosts:
            return raw
        suffix = Path(urllib.parse.unquote(parsed.path)).suffix.lower()
        name = Path(urllib.parse.unquote(parsed.path)).name
        if suffix not in image_extensions and not (suffix in video_extensions and any(name.endswith(allowed) for allowed in allowed_video_names)):
            return raw
        key = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))
        local = downloaded.get(key)
        if local is None:
            return raw
        return os.path.relpath(local, page.parent).replace(os.sep, "/")

    updated = url_re.sub(localize, page_text)
    if updated != page_text:
        rewritten += 1
        page.write_text(updated, encoding="utf-8")

# Fetch Next.js SSG data payloads to preserve route data when the app requests it.
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

# HTTrack normally creates a domain subfolder. Put a working entry point at the archive root.
site_index = root / "pendragoncycle.com" / "index.html"
if site_index.exists():
    (root / "index.html").write_text(
        '<!doctype html><html lang="en"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta http-equiv="refresh" content="0;url=./pendragoncycle.com/index.html">'
        '<title>Pendragon Cycle mirror</title>'
        '<p>Opening the saved site… <a href="./pendragoncycle.com/index.html">Open Pendragon Cycle</a></p></html>',
        encoding="utf-8"
    )

file_count = sum(1 for p in root.rglob("*") if p.is_file())
image_success = sum(1 for u in urls if u in downloaded)
video_success = sum(1 for u in video_urls if u in downloaded)
report = [
    "Pendragon Cycle public-site mirror report",
    f"HTML pages found: {len(pages)}",
    f"Public image URLs discovered: {len(urls)}",
    f"Public images downloaded/localized: {image_success}",
    f"Short-loop video URLs discovered: {len(video_urls)}",
    f"Short-loop videos downloaded/localized: {video_success}",
    f"HTML pages updated with local media paths: {rewritten}",
    f"Next.js route JSON payloads saved: {json_downloads}",
    f"Files in mirror before report: {file_count}",
    "",
    "The long official trailer, HLS manifests, analytics, and tracking are not bundled.",
    "This is a static mirror, not the original server source or backend.",
    "",
    "Asset download failures:",
    *(failed or ["None"]),
    "",
    "Next.js JSON fetch failures:",
    *(json_failures or ["None"]),
]
(root / "MIRROR-REPORT.txt").write_text("\n".join(report) + "\n", encoding="utf-8")
print("\n".join(report[:11]))
