#!/usr/bin/env python3
"""Repair a public HTTrack mirror by fetching static assets referenced only by JS/CSS."""
from __future__ import annotations
import html
import json
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
template_asset_re = re.compile(r"""`(/(?:images|audio|videos|_next/static)/[^`]{1,200})`""")
css_url_re = re.compile(r"""url\(\s*["']?(/(?:images|audio|videos|_next/static)/[^)"'\s]+)""")
manifest_ref_re = re.compile(r"""["'](static/(?:chunks|css)/[^"'\s\\\\]+?\.(?:js|css))["']""")

pages = list(root.rglob("*.html"))
urls: set[str] = set()
loop_video_urls: set[str] = set()
# Map DatoCMS loop-video URLs to their matching public Mux MP4 source.
loop_video_sources: dict[str, tuple[str, str, str]] = {}

def normalized_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))

def collect_loop_video_sources(value: object) -> None:
    if isinstance(value, dict):
        asset_url = value.get("url")
        video = value.get("video")
        if isinstance(asset_url, str) and isinstance(video, dict):
            parsed = urllib.parse.urlsplit(asset_url)
            filename = Path(urllib.parse.unquote(parsed.path)).name
            if (parsed.hostname in asset_hosts and parsed.path.lower().endswith(".mp4")
                    and any(filename.endswith(allowed) for allowed in allowed_video_suffixes)):
                mp4_url = video.get("mp4Url")
                playback_id = video.get("muxPlaybackId")
                streaming_url = video.get("streamingUrl") or ""
                if isinstance(mp4_url, str) and isinstance(playback_id, str):
                    loop_video_sources[normalized_url(asset_url)] = (
                        mp4_url, playback_id,
                        streaming_url if isinstance(streaming_url, str) else "",
                    )
        for child in value.values():
            collect_loop_video_sources(child)
    elif isinstance(value, list):
        for child in value:
            collect_loop_video_sources(child)

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

    # Extract the paired Mux MP4 and playback ID from Next.js page data.
    # DatoCMS currently returns HTTP 422 for these loop-video URLs.
    next_data = re.search(
        r"""<script[^>]+id=["']__NEXT_DATA__["'][^>]*>(.*?)</script>""",
        text,
        flags=re.DOTALL,
    )
    if next_data:
        try:
            collect_loop_video_sources(json.loads(next_data.group(1)))
        except (json.JSONDecodeError, TypeError):
            pass

downloaded_external: dict[str, Path] = {}
failed: list[str] = []
downloaded_js_assets: set[str] = set()

def fetch_to_path(url: str, destination: Path, timeout: int = 35, record_failure: bool = True) -> bool:
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
        if record_failure:
            failed.append(f"{url} [{type(exc).__name__}: {exc}]")
        return False

for url in sorted(urls):
    parsed = urllib.parse.urlsplit(url)
    destination = root / parsed.netloc / Path(urllib.parse.unquote(parsed.path).lstrip("/"))
    if fetch_to_path(url, destination):
        downloaded_external[url] = destination
    time.sleep(0.04)

# Download the short looping videos from their paired Mux MP4 URLs instead of
# the stale DatoCMS file URLs (which return HTTP 422). Keep an external MP4
# fallback in the page data if a local download cannot be completed.
downloaded_loop_videos: dict[str, str] = {}
loop_video_local_sources: dict[str, str] = {}
for url in sorted(loop_video_urls):
    source = loop_video_sources.get(url)
    if not source:
        failed.append(f"{url} [Mux MP4 fallback not found in __NEXT_DATA__]")
        continue
    mp4_url, playback_id, streaming_url = source
    relative_path = f"videos/loops/{playback_id}.mp4"
    destination = site_root / relative_path
    if fetch_to_path(mp4_url, destination, timeout=120):
        local_source = (base_path + "/" + relative_path) if base_path else "/" + relative_path
        downloaded_loop_videos[url] = local_source
    else:
        local_source = mp4_url
    for alias in (url, mp4_url, streaming_url):
        if alias:
            loop_video_local_sources[normalized_url(alias)] = local_source
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

        # Expand dynamically selected image extensions in JavaScript template strings.
        for match in template_asset_re.finditer(source_text):
            template_path = match.group(1).split("?", 1)[0].split("#", 1)[0]
            if template_path.endswith("/") or template_path.count(".."):
                continue
            if "${" not in template_path:
                discovered_paths.add(template_path)
                continue
            placeholders = re.findall(r"\$\{([^{}]+)\}", template_path)
            expanded_paths = [template_path]
            for placeholder in placeholders:
                extensions = list(dict.fromkeys(re.findall(r"avif|webp|png|jpg|jpeg|gif|svg", placeholder, re.IGNORECASE)))
                if not extensions:
                    expanded_paths = []
                    break
                expanded_paths = [
                    candidate.replace("${" + placeholder + "}", extension)
                    for candidate in expanded_paths
                    for extension in extensions
                ]
            for path in expanded_paths:
                if "$" not in path and not path.endswith("/") and ".." not in path:
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
        normalized = normalized_url(candidate)
        loop_local = loop_video_local_sources.get(normalized)
        if loop_local is not None:
            return loop_local
        if parsed.hostname not in asset_hosts:
            return raw
        local = downloaded_external.get(normalized)
        if local is None:
            return raw
        return os.path.relpath(local, page.parent).replace(os.sep, "/")

    updated = url_re.sub(localize_external, text)
    if base_path:
        # Project Pages hosts the mirror below /wme-street/pendragoncycle.com/,
        # so original root-absolute assets must use that prefix.
        for prefix in ("/_next/", "/images/", "/audio/", "/videos/"):
            updated = re.sub(r"""(["'`(=\s])""" + re.escape(prefix), lambda m: m.group(1) + base_path + prefix, updated)
        # Next's client router rewrites anchors to root paths. Force same-origin route clicks
        # through the saved static HTML copies instead of navigating out of the project path.
        if "PENDRAGON_STATIC_ROUTE_FALLBACK" not in updated:
            route_fallback = """
<script id="PENDRAGON_STATIC_ROUTE_FALLBACK">
(function(){
  var prefix = %s;
  var parts = prefix.split('/');
  var repoRoot = parts[1] ? '/' + parts[1] : prefix;
  document.addEventListener('click', function(event) {
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    var target = event.target;
    var anchor = target && target.closest ? target.closest('a[href]') : null;
    if (!anchor || anchor.target === '_blank' || anchor.hasAttribute('download')) return;
    var href = anchor.getAttribute('href') || '';
    if (!href || href.charAt(0) === '#' ||
        href.indexOf('mailto:') === 0 || href.indexOf('tel:') === 0 ||
        href.indexOf('javascript:') === 0 || href.indexOf('data:') === 0) return;

    var targetUrl;
    try { targetUrl = new URL(href, window.location.href); } catch (error) { return; }
    if (targetUrl.origin !== window.location.origin) return;

    var underMirror = targetUrl.pathname === prefix || targetUrl.pathname.indexOf(prefix + '/') === 0;
    var underProject = targetUrl.pathname === repoRoot || targetUrl.pathname.indexOf(repoRoot + '/') === 0;

    if (!underMirror && !underProject && href.charAt(0) === '/') {
      var route = targetUrl.pathname;
      if (route.indexOf('/_next/') === 0 || route.indexOf('/images/') === 0 ||
          route.indexOf('/audio/') === 0 || route.indexOf('/videos/') === 0) return;
      var targetPath;
      if (route === '/') targetPath = prefix + '/index.html';
      else if (route.toLowerCase().slice(-5) === '.html') targetPath = prefix + route;
      else targetPath = prefix + route.replace(/\/+$/, '') + '/index.html';
      targetUrl.pathname = targetPath;
      underMirror = true;
    }

    if (!underMirror && !underProject) return;

    if (underMirror) {
      var localPath = targetUrl.pathname.slice(prefix.length + 1);
      if (localPath.indexOf('_next/') === 0 || localPath.indexOf('images/') === 0 ||
          localPath.indexOf('audio/') === 0 || localPath.indexOf('videos/') === 0) return;
    }

    // HTTrack rewrites internal Next.js links to relative paths such as
    // "story/index.html". Bypass Next's client router and load the saved HTML file.
    event.preventDefault();
    event.stopImmediatePropagation();
    window.location.href = targetUrl.href;
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

# Explicitly fetch textures selected at runtime by the AVIF/WebP detector.
# HTTrack does not reliably discover these because their URLs live in template literals.
dynamic_texture_stems = [
    "images/noise/fbm",
    "images/backdrop/bg2-4k",
    "images/backdrop/preloader_bg",
    "images/backdrop/distort",
    "images/rock/rock-2k",
    "images/rock/rock-normal-2k",
    "images/rock/rock-alpha-2k",
    "images/sword/sword-glow-2k",
    "images/sword/sword-2k",
    "images/sword/sword-alpha-2k",
    "images/sword/sword-normal-2k",
    "images/glyphs/glyph-blur",
    "images/noise-025k",
]
dynamic_texture_failures: list[str] = []
dynamic_texture_fallbacks: dict[str, str] = {}
dynamic_texture_downloads = 0
for stem in dynamic_texture_stems:
    # Check all likely formats first, then explicitly request each missing browser variant.
    for extension in ('.avif', '.webp', '.png'):
        destination = site_root / f'{stem}{extension}'
        if destination.exists() and destination.stat().st_size > 0:
            continue
        if extension == '.png':
            # PNG is a fallback when the source has no AVIF/WebP representation.
            continue
        asset_path = '/' + stem + extension
        if fetch_to_path('https://pendragoncycle.com' + asset_path, destination, timeout=90, record_failure=False):
            dynamic_texture_downloads += 1
        time.sleep(0.04)
    available = [ext for ext in ('.avif', '.webp', '.png')
                 if (site_root / f'{stem}{ext}').exists() and (site_root / f'{stem}{ext}').stat().st_size > 0]
    if not available:
        dynamic_texture_failures.append(stem)
    elif '.webp' in available:
        # WebP is supported broadly and is preferred only when AVIF is missing.
        if '.avif' not in available:
            dynamic_texture_fallbacks[stem] = '.webp'
    elif '.png' in available:
        dynamic_texture_fallbacks[stem] = '.png'
    elif '.avif' in available:
        dynamic_texture_failures.append(stem + ' (AVIF only; no broadly supported fallback)')

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
                r"""(["'`(=\s])""" + re.escape(prefix),
                lambda match: match.group(1) + base_path + prefix,
                updated,
            )
        # If the origin does not provide AVIF but has WebP/PNG, pin this texture
        # to the available local fallback instead of letting the AVIF detector request a 404.
        if base_path and dynamic_texture_fallbacks:
            for stem, extension in dynamic_texture_fallbacks.items():
                texture_prefix = base_path + '/' + stem
                dynamic_pattern = re.escape(texture_prefix) + r'\.\$\{[^{}]+\}'
                updated = re.sub(dynamic_pattern, texture_prefix + extension, updated)

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

# WONGMING_OPENING_TAGLINE_PATCH
# Replace the editable opening line in HTML/JS/Next page data where the text is stored.
tagline_replacements = 0
tagline_variants = (
    ("This is to be Arthur\u2019s story.", "This is to be WongMing story."),
    ("This is to be Arthur's story.", "This is to be WongMing story."),
    ("This is to be Arthur\\u2019s story.", "This is to be WongMing story."),
    ("This is to be Arthur&#39;s story.", "This is to be WongMing story."),
    ("This is to be Arthur&#x27;s story.", "This is to be WongMing story."),
    ("This is to be Arthur&#8217;s story.", "This is to be WongMing story."),
    ("This is to be Arthur\u2019s story", "This is to be WongMing story"),
    ("This is to be Arthur's story", "This is to be WongMing story"),
    ("This is to be Arthur&#39;s story", "This is to be WongMing story"),
)
text_extensions = {".html", ".htm", ".js", ".json", ".css", ".svg", ".txt", ".xml", ".map"}
for source in site_root.rglob("*"):
    if not source.is_file() or source.suffix.lower() not in text_extensions:
        continue
    try:
        original_text = source.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        continue
    updated_text = original_text
    for old_line, new_line in tagline_variants:
        matches = updated_text.count(old_line)
        if matches:
            updated_text = updated_text.replace(old_line, new_line)
            tagline_replacements += matches
    if updated_text != original_text:
        source.write_text(updated_text, encoding="utf-8")

file_count = sum(1 for p in root.rglob("*") if p.is_file())
image_success = sum(1 for url in urls if url in downloaded_external)
video_success = len(downloaded_loop_videos)
report = [
    "Pendragon Cycle public-site mirror report",
    f"HTML pages found: {len(pages)}",
    f"Public image URLs discovered: {len(urls)}",
    f"Public images downloaded/localized: {image_success}",
    f"Short-loop video URLs discovered: {len(loop_video_urls)}",
    f"Short-loop videos downloaded/localized: {video_success}",
    f"Additional JS/CSS-referenced local assets downloaded: {len(downloaded_js_assets)}",
    f"Explicit dynamic texture variants downloaded: {dynamic_texture_downloads}",
    f"Textures pinned to local fallback formats: {len(dynamic_texture_fallbacks)}",
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
    "",
    "Missing dynamic texture sets:",
    *(dynamic_texture_failures or ["None"]),
]
(root / "MIRROR-REPORT.txt").write_text("\n".join(report) + "\n", encoding="utf-8")
print("\n".join(report[:12]))
