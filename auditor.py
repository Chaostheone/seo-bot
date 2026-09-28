"""On-page and technical SEO crawl. Stdlib only."""

from __future__ import annotations

import ipaddress
import re
import socket
from collections import deque
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import urldefrag, urljoin, urlparse
from urllib.request import Request, urlopen

USER_AGENT = "SEOBot/1.0 (+local audit)"
MAX_BYTES = 1_500_000
TIMEOUT = 12


def normalize_url(raw: str) -> str:
    raw = raw.strip()
    if not raw:
        raise ValueError("Enter a URL.")
    if not re.match(r"^https?://", raw, re.I):
        raw = "https://" + raw
    parts = urlparse(raw)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("Use an http or https URL.")
    path = parts.path or "/"
    return parts._replace(path=path, fragment="").geturl()


def _is_public_host(hostname: str) -> None:
    """Block obvious SSRF targets. Localhost is allowed for testing."""
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise ValueError(f"Could not resolve {hostname}.") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_loopback:
            continue
        if (
            ip.is_private
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            raise ValueError("That host is on a private network. Audit a public site.")


def host_key(url: str) -> str:
    host = urlparse(url).hostname or ""
    return host[4:] if host.startswith("www.") else host


class PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.in_title = False
        self.skip_depth = 0
        self.metas: list[dict[str, str]] = []
        self.h1: list[str] = []
        self.headings: list[tuple[str, str]] = []
        self.in_h: str | None = None
        self.h_buf: list[str] = []
        self.links: list[str] = []
        self.images: list[dict[str, str]] = []
        self.canonical = ""
        self.text_parts: list[str] = []
        self.html_lang = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {k.lower(): (v or "") for k, v in attrs}
        if tag == "html":
            self.html_lang = attr.get("lang", "")
        if tag in ("script", "style", "noscript"):
            self.skip_depth += 1
        if tag == "title":
            self.in_title = True
        if tag == "meta":
            self.metas.append(attr)
        if tag == "link" and attr.get("rel", "").lower() == "canonical":
            self.canonical = attr.get("href", "")
        if tag in ("h1", "h2", "h3") and self.skip_depth == 0:
            self.in_h = tag
            self.h_buf = []
        if tag == "a" and attr.get("href"):
            self.links.append(attr["href"])
        if tag == "img":
            self.images.append({"src": attr.get("src", ""), "alt": attr.get("alt", None) if "alt" in attr else ""})
            # distinguish missing alt from empty: HTMLParser gives "" for alt=""
            if "alt" not in attr:
                self.images[-1]["alt"] = None  # type: ignore[assignment]

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "noscript") and self.skip_depth:
            self.skip_depth -= 1
        if tag == "title":
            self.in_title = False
        if tag == self.in_h:
            text = re.sub(r"\s+", " ", "".join(self.h_buf)).strip()
            self.headings.append((tag, text))
            if tag == "h1":
                self.h1.append(text)
            self.in_h = None

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title_parts.append(data)
        if self.in_h:
            self.h_buf.append(data)
        if self.skip_depth == 0 and data.strip():
            self.text_parts.append(data)

    def meta_content(self, name: str) -> str:
        name = name.lower()
        for meta in self.metas:
            key = (meta.get("name") or meta.get("property") or meta.get("http-equiv") or "").lower()
            if key == name:
                return meta.get("content", "")
        return ""


def fetch(url: str) -> tuple[int, str, str, bytes, dict[str, str]]:
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,*/*"})
    try:
        with urlopen(req, timeout=TIMEOUT) as resp:
            status = getattr(resp, "status", 200)
            final = resp.geturl()
            ctype = resp.headers.get("Content-Type", "")
            headers = {k.lower(): v for k, v in resp.headers.items()}
            body = resp.read(MAX_BYTES)
            return status, final, ctype, body, headers
    except HTTPError as exc:
        body = exc.read(MAX_BYTES) if exc.fp else b""
        headers = {k.lower(): v for k, v in exc.headers.items()} if exc.headers else {}
        return exc.code, url, headers.get("content-type", ""), body, headers


def parse_robots(text: str) -> tuple[list[str], list[str]]:
    """Very small robots.txt reader for User-agent: *."""
    disallow: list[str] = []
    sitemaps: list[str] = []
    applies = False
    seen_agent = False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().lower()
        value = value.strip()
        if key == "user-agent":
            applies = value == "*"
            seen_agent = True
        elif key == "disallow" and (applies or not seen_agent):
            if value:
                disallow.append(value)
        elif key == "sitemap":
            sitemaps.append(value)
    return disallow, sitemaps


def robots_blocks(path: str, rules: list[str]) -> bool:
    for rule in rules:
        if path.startswith(rule):
            return True
    return False


def score_page(page: dict) -> tuple[int, list[dict]]:
    issues: list[dict] = []

    def add(severity: str, code: str, message: str, fix: str) -> None:
        weight = {"high": 14, "medium": 8, "low": 3}[severity]
        issues.append({"severity": severity, "code": code, "message": message, "fix": fix, "weight": weight})

    if page["status"] >= 400:
        add("high", "http", f"Page returned HTTP {page['status']}.", "Fix the server error or remove the link that points here.")
    elif page["status"] >= 300:
        add("medium", "redirect", f"Page redirected (HTTP {page['status']}) to {page['final_url']}.", "Link directly to the final URL.")

    if page.get("noindex"):
        add("high", "noindex", "Page is marked noindex.", "Remove the noindex tag if this page should appear in search.")

    title = page.get("title") or ""
    if not title:
        add("high", "title", "Missing <title>.", "Add a unique title of about 50–60 characters that names the page topic.")
    elif len(title) < 15:
        add("medium", "title-short", f"Title is short ({len(title)} characters).", "Expand the title so it describes the page on its own.")
    elif len(title) > 65:
        add("low", "title-long", f"Title is long ({len(title)} characters) and may be cut off.", "Trim the title to about 60 characters.")

    desc = page.get("description") or ""
    if not desc:
        add("high", "description", "Missing meta description.", "Add a 120–160 character summary that matches the page.")
    elif len(desc) < 50:
        add("medium", "description-short", f"Meta description is short ({len(desc)} characters).", "Write a fuller summary, around 120–160 characters.")
    elif len(desc) > 170:
        add("low", "description-long", f"Meta description is long ({len(desc)} characters).", "Shorten it so the full sentence shows in search results.")

    h1 = page.get("h1") or []
    if len(h1) == 0:
        add("high", "h1", "No H1 heading.", "Add one H1 that states what the page is about.")
    elif len(h1) > 1:
        add("medium", "h1-many", f"{len(h1)} H1 headings.", "Keep a single H1 and use H2 for sections.")

    if not page.get("lang"):
        add("low", "lang", "Missing lang on <html>.", 'Set <html lang="en"> (or the page language).')
    if not page.get("viewport"):
        add("medium", "viewport", "Missing viewport meta tag.", 'Add <meta name="viewport" content="width=device-width, initial-scale=1">.')
    if not page.get("canonical"):
        add("medium", "canonical", "Missing canonical link.", "Add a canonical URL so duplicates do not split ranking signals.")
    if page.get("words", 0) < 150 and page["status"] < 400:
        add("medium", "thin", f"Thin main text ({page.get('words', 0)} words).", "Add enough original text to answer the query the page targets.")

    missing_alt = page.get("images_missing_alt", 0)
    if missing_alt:
        add("medium", "alt", f"{missing_alt} image(s) have no alt attribute.", "Describe each informative image. Use empty alt only for decorative images.")

    if not page.get("h2") and page.get("words", 0) > 300:
        add("low", "h2", "Long page with no H2 headings.", "Break the page into sections with H2 headings.")

    if not page.get("og_title"):
        add("low", "og", "Missing Open Graph title.", "Add og:title and og:description so shares look complete.")

    penalty = sum(item["weight"] for item in issues)
    score = max(0, 100 - penalty)
    return score, issues


def analyze_html(url: str, status: int, final_url: str, html: str) -> dict:
    parser = PageParser()
    try:
        parser.feed(html)
    except Exception:
        pass
    text = re.sub(r"\s+", " ", " ".join(parser.text_parts)).strip()
    words = len(text.split()) if text else 0
    robots_meta = parser.meta_content("robots").lower()
    images_missing = sum(1 for img in parser.images if img["alt"] is None)
    page = {
        "url": url,
        "final_url": final_url,
        "status": status,
        "title": re.sub(r"\s+", " ", "".join(parser.title_parts)).strip(),
        "description": parser.meta_content("description").strip(),
        "h1": parser.h1,
        "h2": sum(1 for tag, _ in parser.headings if tag == "h2"),
        "canonical": urljoin(final_url, parser.canonical) if parser.canonical else "",
        "lang": parser.html_lang,
        "viewport": bool(parser.meta_content("viewport")),
        "noindex": "noindex" in robots_meta,
        "og_title": parser.meta_content("og:title"),
        "og_description": parser.meta_content("og:description"),
        "words": words,
        "excerpt": text[:700],
        "images": len(parser.images),
        "images_missing_alt": images_missing,
        "internal_links": 0,
        "external_links": 0,
        "links": parser.links,
    }
    score, issues = score_page(page)
    page["score"] = score
    page["issues"] = issues
    return page


def _sitemap_locs(xml: str, limit: int) -> list[str]:
    return re.findall(r"<loc>\s*([^<]+)\s*</loc>", xml, flags=re.I)[:limit]


def audit_site(start_url: str, max_pages: int = 12, use_sitemap: bool = True) -> dict:
    start_url = normalize_url(start_url)
    _is_public_host(urlparse(start_url).hostname or "")
    origin = host_key(start_url)
    max_pages = max(1, min(int(max_pages), 30))

    root = f"{urlparse(start_url).scheme}://{urlparse(start_url).netloc}"
    robots_rules: list[str] = []
    sitemap_urls: list[str] = []
    robots_ok = False
    try:
        status, _, ctype, body, _ = fetch(root + "/robots.txt")
        if status == 200 and b"<html" not in body[:200].lower():
            robots_ok = True
            text = body.decode("utf-8", "replace")
            robots_rules, sitemap_urls = parse_robots(text)
    except (URLError, TimeoutError, OSError):
        pass

    seeds: list[str] = [start_url]
    if use_sitemap:
        candidates = sitemap_urls or [root + "/sitemap.xml"]
        for sm in candidates[:2]:
            try:
                status, _, _, body, _ = fetch(sm)
                if status == 200:
                    seeds.extend(_sitemap_locs(body.decode("utf-8", "replace"), max_pages))
                    break
            except (URLError, TimeoutError, OSError):
                continue

    queue: deque[str] = deque()
    seen: set[str] = set()
    for seed in seeds:
        clean, _frag = urldefrag(seed)
        parsed = urlparse(clean)
        if host_key(clean) != origin:
            continue
        if parsed.scheme not in ("http", "https"):
            continue
        if clean not in seen:
            seen.add(clean)
            queue.append(clean)

    pages: list[dict] = []
    errors: list[dict] = []
    while queue and len(pages) < max_pages:
        url = queue.popleft()
        path = urlparse(url).path or "/"
        if robots_blocks(path, robots_rules):
            continue
        try:
            status, final, ctype, body, _headers = fetch(url)
        except (URLError, TimeoutError, OSError) as exc:
            errors.append({"url": url, "error": str(exc.reason if isinstance(exc, URLError) else exc)})
            continue
        if "html" not in ctype.lower() and not body[:200].lstrip().lower().startswith(b"<!doctype") and b"<html" not in body[:400].lower():
            continue
        html = body.decode("utf-8", "replace")
        page = analyze_html(url, status, final, html)
        home = host_key(final)
        internal = 0
        external = 0
        for href in page.pop("links"):
            absolute = urljoin(final, href)
            absolute, _frag = urldefrag(absolute)
            parsed = urlparse(absolute)
            if parsed.scheme not in ("http", "https"):
                continue
            if host_key(absolute) == origin or host_key(absolute) == home:
                internal += 1
                if absolute not in seen and len(seen) < max_pages * 4:
                    if not robots_blocks(parsed.path or "/", robots_rules):
                        seen.add(absolute)
                        queue.append(absolute)
            else:
                external += 1
        page["internal_links"] = internal
        page["external_links"] = external
        if internal == 0 and page["status"] < 400:
            page["issues"].append({
                "severity": "low",
                "code": "links",
                "message": "No internal links found.",
                "fix": "Link to related pages on the same site.",
                "weight": 3,
            })
            page["score"] = max(0, page["score"] - 3)
        pages.append(page)

    if not pages:
        raise ValueError("No HTML pages could be fetched. Check the URL and try again.")

    site_issues = []
    if urlparse(start_url).scheme != "https":
        site_issues.append({
            "severity": "high",
            "message": "Site URL is not HTTPS.",
            "fix": "Serve the site over HTTPS and redirect HTTP to HTTPS.",
        })
    if not robots_ok:
        site_issues.append({
            "severity": "medium",
            "message": "No robots.txt found.",
            "fix": "Add /robots.txt and point it at your sitemap.",
        })
    if not sitemap_urls:
        site_issues.append({
            "severity": "medium",
            "message": "No sitemap declared in robots.txt.",
            "fix": "Publish a sitemap and add a Sitemap: line to robots.txt.",
        })

    avg = round(sum(p["score"] for p in pages) / len(pages))
    return {
        "start_url": start_url,
        "pages_crawled": len(pages),
        "score": avg,
        "robots_txt": robots_ok,
        "sitemap": bool(sitemap_urls),
        "site_issues": site_issues,
        "fetch_errors": errors[:10],
        "pages": sorted(pages, key=lambda p: p["score"]),
    }
