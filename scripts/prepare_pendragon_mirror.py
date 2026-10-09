#!/usr/bin/env python3
"""Repair a public HTTrack mirror by fetching static assets referenced only by JS/CSS."""
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
site_root = root / "pendragoncycle.com"
if not site_root.exists():
    raise SystemExit(f"HTTrack site folder not found: {site_root}")

image_extensions = {".jpg", ".jpeg", ".png", ".webp", ".avif", ".gif", ".svg"}
asset_hosts = {"www.datocms-assets.com", "datocms-assets.com", "image.mux.com"}
allowed_video_suffixes = (
    "pendragon-site-home-gif-1.mp4",
    "pendragon-site-story-loop-1.mp4",
    "map1.mp4",
    "pendragon-site-episodes-loop-1.mp4",
    "pendragon-site-character-loop-1.mp4",
    "lineage-v3.mp4",
    "pendragon-subscribe-menu_1280x720.mp4",
)
base_path = os.environ.get("SITE_BASE_PATH", "").rstrip("/")
url_re = re.compile(r'https?://[^"\'\s<>\\\\]+')
quoted_asset_re = re.compile(r"""["'](/(?:images|audio|videos|_next/static)/[^"'\s\)\]\}?,;]{1,200})["']""")
css_url_re = re.compile(r"""url\(\s*["']?(/(?:images|audio|videos|_next/static)/[^)"'\s]+)""")
manifest_ref_re = re.compile(r"""["'](static/(?:chunks|css)/[^"'\s\\\\]+?\.(?:js|css))["']""")

pages = list(root.rglob("*.html"))
urls: set[str] = set()
loop_video_urls: set[str] = set()

# First collect externally hosted public assets embedded in page data.
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
        suffix = Path(urllib.parse.unquote(parsed.path)).suffix.lower()
        name = Path(urllib.parse.unquote(parsed.path)).name
        normalized = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))
        if suffix in image_extensions:
            urls.add(normalized)
        elif suffix == ".mp4" and any(name.endswith(allowed) for allowed in allowed_video_suffixes):
            # DatoCMS names include a timestamp prefix, so match by suffix, not exact name.
            loop_video_urls.add(normalized)

downloaded_external: dict[str, Path] = {}
failed: list[str] = []
downloaded_js_assets: set[str] = set()

def fetch_to_path(url: str, destination: Path, timeout: int = 35) -> bool:
    if destination.exists() and destination.stat().st_size > 0:
        return True
    destination.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; PublicSiteArchive/1.0)",
        "Accept": "*/*",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response, destination.open("wb") as out:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
        if destination.stat().st_size == 0:
            destination.unlink(missing_ok=True)
            raise OSError("empty response")
        return True
    except Exception as exc:
        destination.unlink(missing_ok=True)
        failed.append(f"{url} [{type(exc).__name__}: {exc}]")
        return False

for url in sorted(urls | loop_video_urls):
    parsed = urllib.parse.urlsplit(url)
    destination = root / parsed.netloc / Path(urllib.parse.unquote(parsed.path).lstrip("/"))
    if fetch_to_path(url, destination, timeout=120 if parsed.path.lower().endswith(".mp4") else 35):
        downloaded_external[url] = destination
    time.sleep(0.04)

# Scan JS/CSS repeatedly so late-loaded Next.js chunks are scanned too.
# A first pass can discover page chunks; those chunks can reference additional textures.
downloaded_js_assets: set[str] = set()
attempted_asset_paths: set[str] = set()
scanned_bundles: dict[Path, str] = {}

for scan_round in range(8):
    discovered_paths: set[str] = set()
    source_files = list(site_root.rglob("*.js")) + list(site_root.rglob("*.css"))

    for source in source_files:
        try:
            source_text = source.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if scanned_bundles.get(source) == source_text:
            continue
        scanned_bundles[source] = source_text

        for match in quoted_asset_re.finditer(source_text):
            path = match.group(1).split("?", 1)[0].split("#", 1)[0]
            if "$" in path or path.endswith("/") or path.count(".."):
                continue
            discovered_paths.add(path)

        for match in css_url_re.finditer(source_text):
            path = match.group(1).split("?", 1)[0].split("#", 1)[0]
            if "$" in path or path.endswith("/") or path.count(".."):
                continue
            discovered_paths.add(path)

        for match in manifest_ref_re.finditer(source_text):
            ref = match.group(1)
            if "$" not in ref and ".." not in ref:
                discovered_paths.add("/_next/" + ref)

    pending_paths = sorted(discovered_paths - attempted_asset_paths)
    if not pending_paths:
        break

    downloaded_this_round = 0
    for path in pending_paths:
        attempted_asset_paths.add(path)

        if path.startswith("/audio/"):
            # Howler may declare an extensionless audio base and try WebM then MP3.
            suffixes = [""] if Path(path).suffix else [".webm", ".mp3"]
            asset_paths = [path + suffix for suffix in suffixes]
        elif path.startswith(("/_next/static/", "/images/", "/videos/")):
            asset_paths = [path]
        else:
            continue

        for asset_path in asset_paths:
            destination = site_root / urllib.parse.unquote(asset_path.lstrip("/"))
            if destination.exists() and destination.stat().st_size > 0:
                downloaded_js_assets.add(asset_path)
                continue
            url = "https://pendragoncycle.com" + asset_path
            timeout = 120 if asset_path.lower().endswith(".mp4") else 35
            if fetch_to_path(url, destination, timeout=timeout):
                downloaded_js_assets.add(asset_path)
                downloaded_this_round += 1
            time.sleep(0.03)

    # Newly fetched JavaScript is discovered on the next round.
    if downloaded_this_round == 0:
        break

# Rewrite downloaded external media URLs in HTML/Next page data to local files.
rewritten_pages = 0
for page in pages:
    try:
        text = page.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        continue

    def localize_external(match: re.Match[str]) -> str:
        raw = match.group(0)
        candidate = html.unescape(raw).replace(r"\u0026", "&").rstrip("),;]} ")
        parsed = urllib.parse.urlsplit(candidate)
        if parsed.hostname not in asset_hosts:
            return raw
        normalized = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))
        local = downloaded_external.get(normalized)
        if local is None:
            return raw
        return os.path.relpath(local, page.parent).replace(os.sep, "/")

    updated = url_re.sub(localize_external, text)
    if base_path:
        # Project Pages hosts the mirror below /wme-street/pendragoncycle.com/,
        # so original root-absolute assets must use that prefix.
        for prefix in ("/_next/", "/images/", "/audio/", "/videos/"):
            updated = re.sub(r"""(["'(=\s])""" + re.escape(prefix), lambda m: m.group(1) + base_path + prefix, updated)
        # Next's client router rewrites anchors to root paths. Force same-origin route clicks
        # through the saved static HTML copies instead of navigating out of the project path.
        if "PENDRAGON_STATIC_ROUTE_FALLBACK" not in updated:
            route_fallback = """
<script id="PENDRAGON_STATIC_ROUTE_FALLBACK">
(function(){
  var prefix = %s;
  document.addEventListener('click', function(event) {
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    var target = event.target;
    var anchor = target && target.closest ? target.closest('a[href]') : null;
    if (!anchor || anchor.target === '_blank' || anchor.hasAttribute('download')) return;
    var href = anchor.getAttribute('href') || '';
    if (href.charAt(0) !== '/' || href.indexOf(prefix + '/') === 0) return;
    if (/^\\/(?:_next|images|audio|videos)\\//.test(href)) return;
    var hashAt = href.indexOf('#'), queryAt = href.indexOf('?');
    var cut = href.length;
    if (hashAt >= 0) cut = Math.min(cut, hashAt);
    if (queryAt >= 0) cut = Math.min(cut, queryAt);
    var route = href.slice(0, cut);
    var extra = href.slice(cut);
    var targetPath = route === '/' ? prefix + '/index.html' : prefix + route.replace(/\\/+$/, '') + '/index.html';
    event.preventDefault();
    event.stopImmediatePropagation();
    window.location.href = targetPath + extra;
  }, true);
})();
</script>
""" % ('"' + base_path + '"')
            if "</head>" in updated:
                updated = updated.replace("</head>", route_fallback + "</head>", 1)
            else:
                updated = route_fallback + updated
    if updated != text:
        rewritten_pages += 1
        page.write_text(updated, encoding="utf-8")

# Rewrite every JS/CSS file after recursive downloads, including late-loaded chunks.
rewritten_bundles = 0
if base_path:
    bundle_files = list(site_root.rglob("*.js")) + list(site_root.rglob("*.css"))
    for source in bundle_files:
        try:
            source_text = source.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        updated = source_text
        for prefix in ("/_next/", "/images/", "/audio/", "/videos/"):
            updated = re.sub(
                r"""(["'(=\s])""" + re.escape(prefix),
                lambda match: match.group(1) + base_path + prefix,
                updated,
            )
        if updated != source_text:
            source.write_text(updated, encoding="utf-8")
            rewritten_bundles += 1

# Fetch Next.js SSG JSON so route data is available if the runtime requests it.
build_id = None
for page in pages:
    try:
        match = re.search(r'"buildId"\s*:\s*"([^"]+)"', page.read_text(encoding="utf-8", errors="ignore"))
        if match:
            build_id = match.group(1)
            break
    except OSError:
        pass

json_downloads = 0
json_failures: list[str] = []
if build_id:
    for route in ("index", "story", "map", "episodes", "characters", "lineages", "clans"):
        path = f"/_next/data/{build_id}/{route}.json"
        destination = site_root / path.lstrip("/")
        if fetch_to_path("https://pendragoncycle.com" + path, destination):
            json_downloads += 1
        else:
            json_failures.append(path)
        time.sleep(0.03)

# Add a friendly root entry point for the deployed mirror.
site_index = site_root / "index.html"
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
image_success = sum(1 for url in urls if url in downloaded_external)
video_success = sum(1 for url in loop_video_urls if url in downloaded_external)
report = [
    "Pendragon Cycle public-site mirror report",
    f"HTML pages found: {len(pages)}",
    f"Public image URLs discovered: {len(urls)}",
    f"Public images downloaded/localized: {image_success}",
    f"Short-loop video URLs discovered: {len(loop_video_urls)}",
    f"Short-loop videos downloaded/localized: {video_success}",
    f"Additional JS/CSS-referenced local assets downloaded: {len(downloaded_js_assets)}",
    f"HTML pages updated with local media/base paths: {rewritten_pages}",
    f"JS/CSS bundles updated with project base path: {rewritten_bundles}",
    f"Next.js route JSON payloads saved: {json_downloads}",
    f"Files in mirror before report: {file_count}",
    "",
    "This is a static mirror, not the original server source or backend.",
    "The large official trailer and third-party streaming manifests are not bundled.",
    "",
    "Asset download failures:",
    *(failed or ["None"]),
    "",
    "Next.js JSON fetch failures:",
    *(json_failures or ["None"]),
]
(root / "MIRROR-REPORT.txt").write_text("\n".join(report) + "\n", encoding="utf-8")
print("\n".join(report[:12]))
