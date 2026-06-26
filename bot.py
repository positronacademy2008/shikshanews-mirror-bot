from __future__ import annotations

import hashlib
import html
import io
import logging
import mimetypes
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    import urllib3
except Exception:  # pragma: no cover - urllib3 comes with requests in normal installs.
    urllib3 = None

try:
    from bs4 import BeautifulSoup
except Exception:  # pragma: no cover - handled during startup with a clear error.
    BeautifulSoup = None

try:
    import pikepdf
except Exception:
    pikepdf = None

try:
    from openai import OpenAI
except Exception:
    OpenAI = None


LOGGER = logging.getLogger("improved_bot")

URL_RE = re.compile(r"""(?ix)\b(https?://[^\s<>"')\]]+)""")
TRAILING_URL_PUNCT = ".,;:!?)]]}"

IMAGE_ATTRS = (
    "src",
    "data-src",
    "data-lazy-src",
    "data-original",
    "data-orig-file",
    "data-medium-file",
    "data-large-file",
    "data-url",
)
BAD_IMAGE_HINTS = ("placeholder", "spacer", "blank.gif", "lazyload", "loading.gif")
MEDIA_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}

SPAM_TEXT_HINTS = (
    "betting",
    "casino",
    "aviator",
    "paid promo",
    "paid promotion",
    "sponsored",
    "sponsor",
    "prediction game",
    "earn money",
    "promo code",
    "refer and earn",
)
HARD_SKIP_PHRASES = ("शिक्षा विभाग समाचार",)
DEFAULT_SOURCE_PAGE_HOSTS = ("indianaukrihelp.com",)
DEFAULT_PROTECTED_IMAGE_HOSTS = (
    "tg.i-c-a.su",
    "cdn4.cdn-telegram.org",
)
SPAM_HOST_HINTS = (
    "1xbet",
    "bet365",
    "parimatch",
    "stake.com",
    "dream11",
    "casino",
    "aviator",
)
SUSPICIOUS_INVITE_PATTERNS = (
    "t.me/+",
    "telegram.me/+",
    "t.me/joinchat",
    "telegram.me/joinchat",
)
SOCIAL_HOST_HINTS = (
    "facebook.com",
    "instagram.com",
    "youtube.com",
    "youtu.be",
    "twitter.com",
    "x.com",
    "telegram.me",
    "t.me",
    "whatsapp.com",
)
OFFICIAL_DOMAIN_HINTS = (
    ".gov.in",
    ".nic.in",
    ".ac.in",
    ".edu",
    ".edu.in",
    "rajasthan.gov.in",
    "recruitment",
    "exam",
    "admission",
    "board",
    "university",
    "ssc.nic.in",
    "upsc.gov.in",
    "nta.ac.in",
)
IMPORTANT_LABEL_HINTS = (
    "official",
    "notification",
    "advertisement",
    "apply",
    "online",
    "admit card",
    "result",
    "answer key",
    "syllabus",
    "exam",
    "recruitment",
    "vacancy",
    "eligibility",
    "fee",
    "deadline",
    "circular",
    "pdf",
    "download",
)

AI_SYSTEM_PROMPT = """You write Indian government job, exam, result, and education updates for Positron Academy.
Use clear Hinglish. RECREATE the update in fresh wording — never copy source sentences verbatim.
Keep every fact, date, number, fee, eligibility rule, deadline, exam name, and organisation name accurate.
Do not invent or guess missing details.
Never mention indianaukrihelp.com or other news-aggregator/blog links.
Official government URLs and PDF links are added separately — do not paste random third-party links.
Return plain text for Telegram or clean HTML for WordPress as requested."""


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def parse_int(value: str | None, default: int, minimum: int = 0) -> int:
    if value is None or value == "":
        return default
    try:
        parsed = int(value)
    except ValueError:
        LOGGER.warning("Invalid integer value %r; using %s", value, default)
        return default
    return max(minimum, parsed)


def parse_csv_tuple(value: str | None, default: tuple[str, ...]) -> tuple[str, ...]:
    if value is None or value.strip() == "":
        return default
    parts = tuple(part.strip() for part in value.split(",") if part.strip())
    return parts or default


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


@dataclass(frozen=True)
class Config:
    bot_token: str
    dest_channels: list[str]
    feed_url: str
    wp_url: str = ""
    wp_user: str = ""
    wp_pass: str = ""
    groq_api_key: str = ""
    admin_chat_id: str = ""
    db_file: str = "bot_state.sqlite3"
    max_items_per_run: int = 5
    max_retries: int = 3
    max_run_seconds: int = 240
    allow_insecure_ssl: bool = False
    dry_run: bool = False
    follow_line_tg: str = ""
    follow_line_wa: str = ""
    wp_post_type: str = "pages"
    wp_timeout: int = 25
    wp_max_retries: int = 2
    wp_referer: str = ""
    wp_upload_media: bool = False
    wp_remove_blocked_images: bool = True
    source_page_hosts: tuple[str, ...] = DEFAULT_SOURCE_PAGE_HOSTS
    max_source_pages_per_item: int = 0
    fetch_source_for_links: bool = True
    protected_image_hosts: tuple[str, ...] = DEFAULT_PROTECTED_IMAGE_HOSTS
    skip_message_phrases: tuple[str, ...] = HARD_SKIP_PHRASES
    groq_model: str = "llama-3.1-8b-instant"
    groq_timeout: int = 20
    flood_max_retries: int = 6
    item_delay_seconds: int = 3
    skip_wordpress: bool = False

    @classmethod
    def from_env(cls) -> "Config":
        missing = [name for name in ("BOT_TOKEN", "DEST_CHANNEL", "FEED_URL") if not os.environ.get(name)]
        if missing:
            raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")

        post_type = os.environ.get("WP_POST_TYPE", "pages").strip().lower() or "pages"
        if post_type not in {"pages", "posts"}:
            raise RuntimeError("WP_POST_TYPE must be either 'pages' or 'posts'")

        channels = [part.strip() for part in os.environ["DEST_CHANNEL"].split(",") if part.strip()]
        if not channels:
            raise RuntimeError("DEST_CHANNEL must contain at least one Telegram chat/channel")

        return cls(
            bot_token=os.environ["BOT_TOKEN"].strip(),
            dest_channels=channels,
            feed_url=os.environ["FEED_URL"].strip(),
            wp_url=os.environ.get("WP_URL", "").strip(),
            wp_user=os.environ.get("WP_USER", "").strip(),
            wp_pass=os.environ.get("WP_PASS", "").strip(),
            groq_api_key=os.environ.get("GROQ_API_KEY", "").strip(),
            admin_chat_id=os.environ.get("ADMIN_CHAT_ID", "").strip(),
            db_file=os.environ.get("DB_FILE", "bot_state.sqlite3").strip() or "bot_state.sqlite3",
            max_items_per_run=parse_int(os.environ.get("MAX_ITEMS_PER_RUN"), 5, minimum=1),
            max_retries=parse_int(os.environ.get("MAX_RETRIES"), 3, minimum=0),
            max_run_seconds=parse_int(os.environ.get("MAX_RUN_SECONDS"), 240, minimum=0),
            allow_insecure_ssl=parse_bool(os.environ.get("ALLOW_INSECURE_SSL"), False),
            dry_run=parse_bool(os.environ.get("DRY_RUN"), False),
            follow_line_tg=os.environ.get("FOLLOW_LINE_TG", os.environ.get("FOLLOW_LINE", "")).strip(),
            follow_line_wa=os.environ.get("FOLLOW_LINE_WA", "").strip(),
            wp_post_type=post_type,
            wp_timeout=parse_int(os.environ.get("WP_TIMEOUT"), 25, minimum=5),
            wp_max_retries=parse_int(os.environ.get("WP_MAX_RETRIES"), 2, minimum=1),
            wp_referer=os.environ.get("WP_REFERER", "").strip(),
            wp_upload_media=parse_bool(os.environ.get("WP_UPLOAD_MEDIA"), False),
            wp_remove_blocked_images=parse_bool(os.environ.get("WP_REMOVE_BLOCKED_IMAGES"), True),
            source_page_hosts=parse_csv_tuple(os.environ.get("SOURCE_PAGE_HOSTS"), DEFAULT_SOURCE_PAGE_HOSTS),
            max_source_pages_per_item=parse_int(os.environ.get("MAX_SOURCE_PAGES_PER_ITEM"), 0, minimum=0),
            fetch_source_for_links=parse_bool(os.environ.get("FETCH_SOURCE_FOR_LINKS"), True),
            protected_image_hosts=parse_csv_tuple(
                os.environ.get("PROTECTED_IMAGE_HOSTS"),
                DEFAULT_PROTECTED_IMAGE_HOSTS,
            ),
            skip_message_phrases=parse_csv_tuple(os.environ.get("SKIP_MESSAGE_PHRASES"), HARD_SKIP_PHRASES),
            groq_model=os.environ.get("GROQ_MODEL", "llama-3.1-8b-instant").strip() or "llama-3.1-8b-instant",
            groq_timeout=parse_int(os.environ.get("GROQ_TIMEOUT"), 20, minimum=5),
            flood_max_retries=parse_int(os.environ.get("FLOOD_MAX_RETRIES"), 6, minimum=1),
            item_delay_seconds=parse_int(os.environ.get("ITEM_DELAY_SECONDS"), 3, minimum=0),
            skip_wordpress=parse_bool(os.environ.get("SKIP_WORDPRESS"), False),
        )

    @property
    def verify_ssl(self) -> bool:
        return not self.allow_insecure_ssl

    @property
    def wordpress_ready(self) -> bool:
        return bool(self.wp_url and self.wp_user and self.wp_pass)


@dataclass
class LinkInfo:
    label: str
    href: str


@dataclass
class FeedItem:
    guid: str
    title: str
    text: str
    html_content: str
    source_url: str
    enclosure_url: str = ""
    enclosure_type: str = ""
    content_hash: str = ""


def build_session(config: Config) -> requests.Session:
    if config.allow_insecure_ssl and urllib3:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        LOGGER.warning("ALLOW_INSECURE_SSL=true; HTTPS certificate verification is disabled.")

    retry = Retry(
        total=1,
        connect=1,
        read=1,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "HEAD"}),
        raise_on_status=False,
    )
    session = requests.Session()
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


FLOOD_WAIT_RE = re.compile(r"FLOOD_WAIT_(\d+)", re.I)


def flood_wait_seconds(response: requests.Response | None = None, error_text: str = "") -> int:
    chunks: list[str] = []
    if response is not None:
        chunks.append(response.text or "")
        if response.reason:
            chunks.append(response.reason)
    if error_text:
        chunks.append(error_text)
    for chunk in chunks:
        match = FLOOD_WAIT_RE.search(chunk)
        if match:
            return max(1, int(match.group(1)))
    return 0


def request_with_flood_retry(
    session: requests.Session,
    method: str,
    url: str,
    *,
    max_attempts: int = 6,
    label: str = "request",
    **kwargs: Any,
) -> requests.Response:
    last_response: requests.Response | None = None
    last_error = ""
    for attempt in range(1, max_attempts + 1):
        try:
            response = session.request(method, url, **kwargs)
            last_response = response
            if response.status_code == 429:
                wait = flood_wait_seconds(response)
                if wait:
                    LOGGER.warning(
                        "Rate limited (%ss) on %s; sleeping and retrying %s/%s",
                        wait,
                        label,
                        attempt,
                        max_attempts,
                    )
                    time.sleep(wait + 1)
                    continue
            response.raise_for_status()
            return response
        except requests.HTTPError as exc:
            last_error = str(exc)
            wait = flood_wait_seconds(last_response, last_error)
            if wait and attempt < max_attempts:
                LOGGER.warning(
                    "HTTP error with flood wait %ss on %s; retrying %s/%s",
                    wait,
                    label,
                    attempt,
                    max_attempts,
                )
                time.sleep(wait + 1)
                continue
            raise
    if last_response is not None:
        last_response.raise_for_status()
    raise RuntimeError(last_error or f"Request failed after {max_attempts} attempts: {label}")


def check_telegram_response(response: requests.Response, method: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(f"Telegram {method} returned invalid JSON: {response.text[:300]}") from exc
    if not payload.get("ok"):
        description = payload.get("description") or payload
        raise RuntimeError(f"Telegram {method} rejected: {description}")
    return payload


def default_headers(referer: str = "") -> dict[str, str]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    if referer:
        headers["Referer"] = referer
    return headers


def origin_from_url(url: str) -> str:
    parsed = urlparse(url or "")
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"


def hostname_from_url(url: str) -> str:
    parsed = urlparse(url or "")
    return (parsed.hostname or "").lower().removeprefix("www.")


def same_hostname(left_url: str, right_url: str) -> bool:
    left = hostname_from_url(left_url)
    right = hostname_from_url(right_url)
    return bool(left and right and left == right)


def referer_for_download(source_url: str, referer: str = "") -> str:
    return referer if referer and same_hostname(source_url, referer) else ""


def make_soup(markup: str, parser: str = "html.parser") -> Any:
    if BeautifulSoup is None:
        raise RuntimeError("beautifulsoup4 is required. Install it with: pip install beautifulsoup4")
    try:
        return BeautifulSoup(markup or "", parser)
    except Exception:
        return BeautifulSoup(markup or "", "html.parser")


def clean_url(raw_url: str) -> str:
    return (raw_url or "").strip().rstrip(TRAILING_URL_PUNCT)


def canonical_url(url: str) -> str:
    return clean_url(url).split("#", 1)[0].rstrip("/")


def safe_url(raw_url: str | None, base_url: str = "") -> str:
    if not raw_url:
        return ""
    raw_url = html.unescape(str(raw_url).strip())
    if not raw_url or raw_url.startswith(("data:", "blob:", "javascript:", "mailto:", "tel:", "#")):
        return ""
    return clean_url(urljoin(base_url, raw_url))


def extract_urls(text: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for match in URL_RE.findall(text or ""):
        url = clean_url(match)
        key = url.rstrip("/")
        if url and key not in seen:
            seen.add(key)
            urls.append(url)
    return urls


def host_matches(url: str, hosts: Iterable[str]) -> bool:
    parsed = urlparse(url or "")
    hostname = (parsed.netloc or parsed.path.split("/", 1)[0]).lower()
    hostname = hostname.split("@")[-1].split(":", 1)[0].removeprefix("www.")
    for raw_host in hosts:
        host = (raw_host or "").strip().lower()
        if not host:
            continue
        if "://" in host:
            host = urlparse(host).netloc.lower()
        host = host.split("/", 1)[0].split(":", 1)[0].removeprefix("www.")
        if hostname == host or hostname.endswith("." + host):
            return True
    return False


def is_spam_url(url: str) -> bool:
    lower = (url or "").lower()
    if any(pattern in lower for pattern in SUSPICIOUS_INVITE_PATTERNS):
        return True
    host = urlparse(url).netloc.lower()
    return any(hint in host or hint in lower for hint in SPAM_HOST_HINTS)


def line_has_spam(text: str) -> bool:
    lower = (text or "").lower()
    return any(hint in lower for hint in SPAM_TEXT_HINTS) or any(pattern in lower for pattern in SUSPICIOUS_INVITE_PATTERNS)


def message_has_skip_phrase(item: FeedItem, phrases: Iterable[str]) -> str:
    haystack = normalize_whitespace(
        "\n".join([item.title or "", item.text or "", strip_tags(item.html_content or "")])
    )
    for phrase in phrases:
        if phrase and phrase in haystack:
            return phrase
    return ""


def looks_like_ad_message(text: str) -> bool:
    clean = normalize_whitespace(text or "")
    if not clean:
        return False
    lower = clean.lower()
    if any(hint in lower for hint in ("betting", "casino", "aviator", "paid promo", "paid promotion")):
        return True

    lines = [line.strip() for line in clean.splitlines() if line.strip()]
    if not lines:
        return False
    spam_lines = [line for line in lines if line_has_spam(line) or any(is_spam_url(url) for url in extract_urls(line))]
    if not spam_lines:
        return False

    useful_terms = (
        "result",
        "admit card",
        "vacancy",
        "recruitment",
        "exam",
        "notification",
        "apply",
        "date",
        "fee",
        "eligibility",
        "deadline",
        "रिजल्ट",
        "भर्ती",
        "परीक्षा",
        "आवेदन",
    )
    useful_hits = sum(1 for term in useful_terms if term in lower)
    return len(spam_lines) / max(len(lines), 1) >= 0.5 and useful_hits == 0


def strip_tags(value: str) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.I)
    value = re.sub(r"<.*?>", "", value, flags=re.S)
    return normalize_whitespace(value)


def normalize_whitespace(value: str) -> str:
    value = html.unescape(value or "")
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"[\u200b\u200c\u200d\ufeff]", "", value)
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n[ \t]+", "\n", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def remove_prefixes(value: str) -> str:
    return re.sub(r"^\[(?:Photo|Media|Video|Document)\]\s*", "", value or "", flags=re.I).strip()


def remove_spam_lines(text: str) -> str:
    kept: list[str] = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            kept.append("")
            continue
        urls = extract_urls(line)
        if line_has_spam(line) or any(is_spam_url(url) for url in urls):
            LOGGER.info("Removed spam/promotional line: %s", line[:120])
            continue
        kept.append(raw_line)
    return normalize_whitespace("\n".join(kept))


def remove_spam_urls_from_text(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        url = clean_url(match.group(1))
        return "" if is_spam_url(url) else url

    return remove_spam_lines(URL_RE.sub(replace, text or ""))


def html_to_text_with_links(markup: str, base_url: str = "") -> str:
    soup = make_soup(markup or "", "html.parser")
    for element in soup(["script", "style", "noscript", "iframe"]):
        element.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for li in soup.find_all("li"):
        li.insert_before("\n- ")
    for tag in soup.find_all(["p", "div", "section", "article", "tr", "h1", "h2", "h3", "h4"]):
        tag.insert_before("\n")
        tag.insert_after("\n")
    for link in soup.find_all("a", href=True):
        href = safe_url(link.get("href"), base_url)
        label = normalize_whitespace(link.get_text(" ", strip=True))
        if not href or is_spam_url(href):
            link.unwrap()
            continue
        if href not in label:
            link.append(f" ({href})")
    return remove_spam_urls_from_text(normalize_whitespace(soup.get_text("\n", strip=False)))


def text_to_html(text: str) -> str:
    paragraphs: list[str] = []
    for block in re.split(r"\n{2,}", normalize_whitespace(text or "")):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        escaped = "<br>".join(linkify_text(html.escape(line)) for line in lines)
        paragraphs.append(f"<p>{escaped}</p>")
    return "\n".join(paragraphs)


def linkify_text(escaped_text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        url = clean_url(html.unescape(match.group(1)))
        safe_href = html.escape(url, quote=True)
        return f'<a href="{safe_href}" target="_blank" rel="nofollow noopener">{html.escape(url)}</a>'

    return URL_RE.sub(replace, escaped_text)


def normalize_links(soup_or_tag: Any, base_url: str = "") -> None:
    if soup_or_tag is None or not hasattr(soup_or_tag, "find_all"):
        return
    for link in soup_or_tag.find_all("a"):
        href = safe_url(link.get("href"), base_url)
        if not href or is_spam_url(href):
            link.unwrap()
            continue
        link["href"] = href
        link["target"] = "_blank"
        link["rel"] = "nofollow noopener"


def parse_srcset(srcset_value: str | None, base_url: str = "") -> str:
    candidates: list[tuple[int, str]] = []
    for item in (srcset_value or "").split(","):
        parts = item.strip().split()
        if not parts:
            continue
        candidate_url = safe_url(parts[0], base_url)
        if not candidate_url:
            continue
        score = 0
        if len(parts) > 1:
            descriptor = parts[1]
            if descriptor.endswith("w"):
                try:
                    score = int(descriptor[:-1])
                except ValueError:
                    score = 0
            elif descriptor.endswith("x"):
                try:
                    score = int(float(descriptor[:-1]) * 1000)
                except ValueError:
                    score = 0
        candidates.append((score, candidate_url))
    if not candidates:
        return ""
    candidates.sort(key=lambda item: item[0])
    return candidates[-1][1]


def looks_like_real_image(url: str) -> bool:
    lower = (url or "").lower()
    if not lower or lower.startswith("data:") or is_spam_url(lower):
        return False
    return not any(hint in lower for hint in BAD_IMAGE_HINTS)


def image_candidate_from_tag(img: Any, base_url: str = "") -> str:
    candidates: list[str] = []
    for attr in IMAGE_ATTRS:
        value = img.get(attr)
        if value:
            candidates.append(safe_url(value, base_url))
    for attr in ("srcset", "data-srcset"):
        value = img.get(attr)
        if value:
            candidates.append(parse_srcset(value, base_url))
    parent = img.parent
    if parent and getattr(parent, "name", "") == "picture":
        for source in parent.find_all("source"):
            candidates.append(parse_srcset(source.get("srcset") or source.get("data-srcset"), base_url))
    for candidate in candidates:
        if looks_like_real_image(candidate):
            return candidate
    return ""


def merge_style(existing_style: str | None, required_style: str) -> str:
    existing = (existing_style or "").strip()
    if existing and not existing.endswith(";"):
        existing += ";"
    return (existing + " " + required_style).strip()


def sanitize_html_content(markup: str, base_url: str = "") -> str:
    soup = make_soup(markup or "", "html.parser")
    for element in soup(["script", "style", "noscript", "iframe", "form", "button"]):
        element.decompose()
    normalize_links(soup, base_url)
    for text_node in list(soup.find_all(string=True)):
        parent_name = getattr(text_node.parent, "name", "")
        if parent_name in {"a", "script", "style", "textarea"}:
            continue
        original = str(text_node)
        if URL_RE.search(original):
            linked = linkify_text(html.escape(original))
            text_node.replace_with(make_soup(linked, "html.parser"))
    for link in list(soup.find_all("a", href=True)):
        if is_spam_url(link.get("href", "")) or line_has_spam(link.get_text(" ", strip=True)):
            link.unwrap()
    return str(soup)


def link_label(link: Any) -> str:
    text = normalize_whitespace(link.get_text(" ", strip=True))
    title = normalize_whitespace(link.get("title") or "")
    aria = normalize_whitespace(link.get("aria-label") or "")
    label = text or title or aria
    if not label:
        href = link.get("href") or ""
        parsed = urlparse(href)
        label = parsed.netloc or href
    return label[:100]


def is_important_link(label: str, href: str) -> bool:
    if not href or is_spam_url(href):
        return False
    parsed = urlparse(href)
    if parsed.scheme not in {"http", "https"}:
        return False
    lower_href = href.lower()
    lower_label = label.lower()
    if any(host in lower_href for host in SOCIAL_HOST_HINTS):
        return False
    if any(word in lower_label for word in ("privacy", "terms", "advertise", "contact", "about us")):
        return False
    official_domain = any(hint in lower_href for hint in OFFICIAL_DOMAIN_HINTS)
    useful_label = any(hint in lower_label or hint in lower_href for hint in IMPORTANT_LABEL_HINTS)
    return official_domain or useful_label


def dedupe_links(links: Iterable[LinkInfo], limit: int = 24) -> list[LinkInfo]:
    output: list[LinkInfo] = []
    seen: set[str] = set()
    for link in links:
        href = safe_url(link.href)
        if not href:
            continue
        key = href.split("#", 1)[0].rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        output.append(LinkInfo(label=link.label.strip()[:100] or urlparse(href).netloc, href=href))
        if len(output) >= limit:
            break
    return output


def extract_links_from_html(markup: str, base_url: str = "") -> list[LinkInfo]:
    soup = make_soup(markup or "", "html.parser")
    links: list[LinkInfo] = []
    for link in soup.find_all("a", href=True):
        href = safe_url(link.get("href"), base_url)
        if not href or is_spam_url(href):
            continue
        links.append(LinkInfo(label=link_label(link), href=href))
    for url in extract_urls(soup.get_text(" ", strip=True)):
        if not is_spam_url(url):
            links.append(LinkInfo(label=urlparse(url).netloc or "Source", href=url))
    return dedupe_links(links)


def extract_important_links(markup: str, base_url: str = "", extra_urls: Iterable[str] = ()) -> list[LinkInfo]:
    links = extract_links_from_html(markup, base_url)
    for url in extra_urls:
        if url and not is_spam_url(url):
            links.append(LinkInfo(label=urlparse(url).netloc or "Source", href=url))
    return dedupe_links((link for link in links if is_important_link(link.label, link.href)), limit=24)


def apply_link_replacements_text(text: str, replacements: dict[str, str]) -> str:
    if not text or not replacements:
        return text

    def replace(match: re.Match[str]) -> str:
        url = clean_url(match.group(1))
        return replacements.get(canonical_url(url), url)

    return URL_RE.sub(replace, text)


def apply_link_replacements_html(markup: str, replacements: dict[str, str], base_url: str = "") -> str:
    if not markup or not replacements:
        return markup
    soup = make_soup(markup, "html.parser")
    for link in soup.find_all("a", href=True):
        href = safe_url(link.get("href"), base_url)
        replacement = replacements.get(canonical_url(href))
        if replacement:
            link["href"] = replacement
    for text_node in list(soup.find_all(string=True)):
        parent_name = getattr(text_node.parent, "name", "")
        if parent_name in {"a", "script", "style", "textarea"}:
            continue
        original = str(text_node)
        replaced = apply_link_replacements_text(original, replacements)
        if replaced != original:
            text_node.replace_with(replaced)
    return str(soup)


def important_links_block(links: list[LinkInfo]) -> str:
    if not links:
        return ""
    items = []
    for link in links:
        href = html.escape(link.href, quote=True)
        label = html.escape(link.label or link.href)
        items.append(f'<li><a href="{href}" target="_blank" rel="nofollow noopener">{label}</a></li>')
    return (
        '<section class="important-links">'
        "<h2>Important Links</h2>"
        f"<ul>{''.join(items)}</ul>"
        "</section>"
    )


def source_block(source_url: str) -> str:
    if not source_url:
        return ""
    href = html.escape(source_url, quote=True)
    label = html.escape(urlparse(source_url).netloc or source_url)
    return (
        '<section class="source-link">'
        "<h2>Official/Source Link</h2>"
        f'<p><a href="{href}" target="_blank" rel="nofollow noopener">{label}</a></p>'
        "</section>"
    )


def select_article(soup: Any) -> Any:
    candidates = [
        soup.find("article"),
        soup.find(class_=re.compile(r"entry-content|post-content|post-body|content-area|article-content", re.I)),
        soup.find(id=re.compile(r"post|article|content", re.I)),
        soup.find("main"),
        soup.find("body"),
    ]
    for candidate in candidates:
        if candidate:
            return candidate
    return None


def clean_layout_noise(soup: Any) -> None:
    for element in soup(["script", "style", "noscript", "iframe", "form"]):
        element.decompose()
    for element in soup.find_all(["nav", "footer", "header", "aside"]):
        element.decompose()


class StateStore:
    def __init__(self, db_file: str) -> None:
        self.db_file = db_file
        self.conn = sqlite3.connect(db_file)
        self.conn.row_factory = sqlite3.Row
        self.init_schema()

    def init_schema(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS items (
                guid TEXT PRIMARY KEY,
                title TEXT,
                source_url TEXT,
                content_hash TEXT,
                status TEXT CHECK(status IN ('pending', 'published', 'failed', 'skipped')),
                wp_link TEXT,
                error TEXT,
                created_at TEXT,
                updated_at TEXT,
                retries INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(items)")}
        if "retries" not in columns:
            self.conn.execute("ALTER TABLE items ADD COLUMN retries INTEGER NOT NULL DEFAULT 0")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def get(self, guid: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM items WHERE guid = ?", (guid,)).fetchone()

    def upsert_pending(self, item: FeedItem) -> None:
        now = utc_now()
        self.conn.execute(
            """
            INSERT INTO items (
                guid, title, source_url, content_hash, status, wp_link, error, created_at, updated_at, retries
            )
            VALUES (?, ?, ?, ?, 'pending', '', '', ?, ?, 0)
            ON CONFLICT(guid) DO UPDATE SET
                title = excluded.title,
                source_url = excluded.source_url,
                content_hash = excluded.content_hash,
                updated_at = excluded.updated_at
            WHERE items.status NOT IN ('published', 'skipped')
            """,
            (item.guid, item.title, item.source_url, item.content_hash, now, now),
        )
        self.conn.commit()

    def set_wp_link(self, guid: str, wp_link: str) -> None:
        self.conn.execute(
            "UPDATE items SET wp_link = ?, updated_at = ? WHERE guid = ?",
            (wp_link or "", utc_now(), guid),
        )
        self.conn.commit()

    def mark_published(self, guid: str, wp_link: str = "") -> None:
        self.conn.execute(
            """
            UPDATE items
            SET status = 'published', wp_link = COALESCE(NULLIF(?, ''), wp_link), error = '', updated_at = ?
            WHERE guid = ?
            """,
            (wp_link or "", utc_now(), guid),
        )
        self.conn.commit()

    def mark_failed(self, guid: str, error: str) -> None:
        self.conn.execute(
            """
            UPDATE items
            SET status = 'failed', error = ?, retries = COALESCE(retries, 0) + 1, updated_at = ?
            WHERE guid = ?
            """,
            (error[:1000], utc_now(), guid),
        )
        self.conn.commit()

    def mark_skipped(self, guid: str, reason: str) -> None:
        self.conn.execute(
            """
            UPDATE items
            SET status = 'skipped', error = ?, updated_at = ?
            WHERE guid = ?
            """,
            (reason[:1000], utc_now(), guid),
        )
        self.conn.commit()

    def list_published_without_wp_link(self, limit: int = 10) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT guid, title, source_url, wp_link
            FROM items
            WHERE status = 'published' AND COALESCE(wp_link, '') = ''
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()


class TelegramClient:
    def __init__(self, config: Config, session: requests.Session) -> None:
        self.config = config
        self.session = session

    def _api_url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.config.bot_token}/{method}"

    def send_admin_critical(self, message: str) -> None:
        if not self.config.admin_chat_id:
            return
        try:
            self.send_text(self.config.admin_chat_id, f"Critical bot error:\n{message[:3500]}", disable_preview=True)
        except Exception as exc:
            LOGGER.error("Could not notify ADMIN_CHAT_ID: %s", exc)

    def send_text(self, chat_id: str, text: str, disable_preview: bool = False) -> None:
        text = trim_preserving_urls(text, 3900)
        if self.config.dry_run:
            LOGGER.info("[DRY_RUN] Would send text to %s: %s", chat_id, text[:200])
            return
        LOGGER.info("Sending Telegram text to %s", chat_id)
        response = request_with_flood_retry(
            self.session,
            "POST",
            self._api_url("sendMessage"),
            max_attempts=self.config.flood_max_retries,
            label=f"sendMessage:{chat_id}",
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": disable_preview},
            timeout=20,
        )
        payload = check_telegram_response(response, "sendMessage")
        LOGGER.info(
            "Telegram text delivered to %s (message_id=%s)",
            chat_id,
            payload.get("result", {}).get("message_id"),
        )

    def send_photo(self, chat_id: str, photo_bytes: bytes, caption: str, content_type: str) -> None:
        content_type = normalize_mime(content_type)
        if content_type not in MEDIA_IMAGE_TYPES:
            raise ValueError(f"Unsupported image MIME for Telegram photo: {content_type or 'unknown'}")
        caption = trim_preserving_urls(caption, 900)
        if self.config.dry_run:
            LOGGER.info("[DRY_RUN] Would send photo to %s with caption: %s", chat_id, caption[:200])
            return
        LOGGER.info("Sending Telegram photo to %s", chat_id)
        ext = mimetypes.guess_extension(content_type) or ".jpg"
        files = {"photo": (f"image{ext}", photo_bytes, content_type)}
        data = {"chat_id": chat_id, "caption": caption}
        response = request_with_flood_retry(
            self.session,
            "POST",
            self._api_url("sendPhoto"),
            max_attempts=self.config.flood_max_retries,
            label=f"sendPhoto:{chat_id}",
            data=data,
            files=files,
            timeout=60,
        )
        payload = check_telegram_response(response, "sendPhoto")
        LOGGER.info(
            "Telegram photo delivered to %s (message_id=%s)",
            chat_id,
            payload.get("result", {}).get("message_id"),
        )

    def send_document(self, chat_id: str, document_bytes: bytes, filename: str, caption: str) -> None:
        caption = trim_preserving_urls(caption, 900)
        if self.config.dry_run:
            LOGGER.info("[DRY_RUN] Would send document to %s with caption: %s", chat_id, caption[:200])
            return
        LOGGER.info("Sending Telegram document to %s", chat_id)
        files = {"document": (filename, document_bytes, "application/pdf")}
        data = {"chat_id": chat_id, "caption": caption}
        response = request_with_flood_retry(
            self.session,
            "POST",
            self._api_url("sendDocument"),
            max_attempts=self.config.flood_max_retries,
            label=f"sendDocument:{chat_id}",
            data=data,
            files=files,
            timeout=75,
        )
        payload = check_telegram_response(response, "sendDocument")
        LOGGER.info(
            "Telegram document delivered to %s (message_id=%s)",
            chat_id,
            payload.get("result", {}).get("message_id"),
        )


class WordPressClient:
    def __init__(self, config: Config, session: requests.Session) -> None:
        self.config = config
        self.session = session
        self.upload_cache: dict[str, str] = {}
        self.media_upload_available = config.wp_upload_media

    @property
    def ready(self) -> bool:
        return self.config.wordpress_ready

    def api_root(self) -> str:
        clean = self.config.wp_url.rstrip("/")
        marker = "/wp-json/wp/v2"
        if marker in clean:
            return clean.split(marker, 1)[0] + marker
        return clean + marker

    def endpoint(self, resource: str) -> str:
        return f"{self.api_root()}/{resource.strip('/')}"

    def api_headers(self, headers: dict[str, str] | None = None) -> dict[str, str]:
        final_headers = {
            "User-Agent": default_headers()["User-Agent"],
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "en-US,en;q=0.9,hi;q=0.8",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }
        origin = origin_from_url(self.config.wp_url)
        if origin:
            final_headers["Origin"] = origin
            final_headers["Referer"] = self.config.wp_referer or f"{origin}/wp-admin/"
        final_headers.update(headers or {})
        return final_headers

    def request_json(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        headers = self.api_headers(kwargs.pop("headers", {}))
        timeout = kwargs.pop("timeout", self.config.wp_timeout)
        if isinstance(timeout, (int, float)):
            request_timeout: Any = (min(25, int(timeout)), int(timeout))
        else:
            request_timeout = timeout
        attempts = self.config.wp_max_retries
        last_error = ""
        for attempt in range(1, attempts + 1):
            try:
                response = self.session.request(
                    method,
                    url,
                    auth=(self.config.wp_user, self.config.wp_pass),
                    headers=headers,
                    timeout=request_timeout,
                    verify=self.config.verify_ssl,
                    **kwargs,
                )
                if response.status_code in (200, 201):
                    return response.json()
                last_error = f"HTTP {response.status_code}: {response.text[:300]}"
                if response.status_code not in {429, 500, 502, 503, 504}:
                    break
            except Exception as exc:
                last_error = str(exc)
            LOGGER.warning("WordPress API attempt %s/%s failed: %s", attempt, attempts, last_error)
            if attempt < attempts:
                time.sleep(min(2 ** attempt, 8))
        raise RuntimeError(f"WordPress API failed: {last_error}")

    def upload_media_bytes(self, media_bytes: bytes, source_url: str, content_type: str, alt_text: str = "") -> str:
        if not self.ready:
            return ""
        if not self.media_upload_available:
            return ""
        content_type = normalize_mime(content_type) or mimetypes.guess_type(source_url)[0] or "image/jpeg"
        if not content_type.startswith("image/"):
            LOGGER.warning("Skipping WordPress upload for non-image media: %s", source_url)
            return ""
        filename = guess_filename(source_url, content_type)
        headers = {
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Type": content_type,
        }
        try:
            payload = self.request_json(
                "POST",
                self.endpoint("media"),
                headers=headers,
                data=media_bytes,
                timeout=self.config.wp_timeout,
            )
        except RuntimeError as exc:
            error = str(exc)
            if "HTTP 406" in error or "Mod_Security" in error:
                self.media_upload_available = False
                LOGGER.warning(
                    "WordPress media upload is blocked by ModSecurity; skipping media uploads for this run. "
                    "Set WP_UPLOAD_MEDIA=false or whitelist /wp-json/wp/v2/media on your own site."
                )
            raise
        media_url = payload.get("source_url") or payload.get("guid", {}).get("rendered", "")
        media_id = payload.get("id")
        if alt_text and media_id:
            try:
                self.request_json(
                    "POST",
                    self.endpoint(f"media/{media_id}"),
                    json={"alt_text": alt_text[:120]},
                    timeout=min(self.config.wp_timeout, 20),
                )
            except Exception as exc:
                LOGGER.warning("Could not update WordPress media alt text: %s", exc)
        return media_url

    def upload_media_from_url(self, source_url: str, referer: str = "", alt_text: str = "") -> str:
        if not source_url:
            return ""
        if source_url in self.upload_cache:
            return self.upload_cache[source_url]
        if not self.media_upload_available:
            self.upload_cache[source_url] = ""
            return ""
        try:
            headers = default_headers(referer_for_download(source_url, referer))
            response = self.session.get(
                source_url,
                headers=headers,
                timeout=45,
                verify=self.config.verify_ssl,
            )
            if response.status_code == 403 and "Referer" in headers and "referer" in response.text.lower():
                response = self.session.get(
                    source_url,
                    headers=default_headers(),
                    timeout=45,
                    verify=self.config.verify_ssl,
                )
            response.raise_for_status()
            content_type = normalize_mime(response.headers.get("Content-Type", ""))
            if not content_type.startswith("image/"):
                guessed = mimetypes.guess_type(source_url)[0] or ""
                if not guessed.startswith("image/"):
                    LOGGER.warning("Skipping non-image media from %s (%s)", source_url, content_type or "unknown")
                    self.upload_cache[source_url] = ""
                    return ""
                content_type = guessed
            uploaded = self.upload_media_bytes(response.content, source_url, content_type, alt_text)
            self.upload_cache[source_url] = uploaded
            return uploaded
        except Exception as exc:
            LOGGER.warning("WordPress image upload failed for %s: %s", source_url, exc)
            self.upload_cache[source_url] = ""
            return ""

    def is_protected_image_url(self, url: str) -> bool:
        return host_matches(url, self.config.protected_image_hosts)

    def normalize_images(self, soup_or_tag: Any, base_url: str = "") -> None:
        for source in soup_or_tag.find_all("source"):
            if source.parent and getattr(source.parent, "name", "") == "picture":
                source.decompose()
                continue
            best_url = parse_srcset(source.get("srcset") or source.get("data-srcset"), base_url)
            if best_url:
                source["srcset"] = best_url

        for img in soup_or_tag.find_all("img"):
            source_url = image_candidate_from_tag(img, base_url)
            if not source_url:
                continue
            final_url = source_url
            uploaded_url = ""
            if self.ready and self.media_upload_available:
                uploaded_url = self.upload_media_from_url(source_url, referer=base_url, alt_text=img.get("alt", ""))
            if uploaded_url:
                final_url = uploaded_url
            elif self.config.wp_remove_blocked_images and self.is_protected_image_url(source_url):
                container = img.parent if getattr(img.parent, "name", "") in {"figure", "picture"} else img
                container.decompose()
                continue
            img["src"] = final_url
            img["loading"] = "lazy"
            img["decoding"] = "async"
            img["style"] = merge_style(img.get("style"), "max-width:100%; height:auto;")
            for attr in list(img.attrs):
                if attr.startswith("data-") or attr in {"srcset", "sizes"}:
                    del img[attr]

    def publish(self, title: str, content_html: str, base_url: str = "") -> str:
        if not self.ready:
            LOGGER.info("WordPress credentials are not configured; skipping WordPress publish.")
            return ""
        if self.config.dry_run:
            LOGGER.info("[DRY_RUN] Would publish %s to WordPress.", self.config.wp_post_type)
            return ""

        soup = make_soup(content_html, "html.parser")
        normalize_links(soup, base_url)
        self.normalize_images(soup, base_url)
        final_content = str(soup)
        data = {
            "title": title[:180],
            "content": final_content,
            "status": "publish",
            "slug": f"update-{int(time.time() * 1000)}",
        }
        LOGGER.info("Publishing WordPress %s: %s", self.config.wp_post_type[:-1], title[:80])
        payload = self.request_json(
            "POST",
            self.endpoint(self.config.wp_post_type),
            headers={"Content-Type": "application/json"},
            json=data,
            timeout=self.config.wp_timeout,
        )
        return payload.get("link", "")


class AIRewriter:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.client: Any = None
        self.disabled_reason = ""
        if not config.groq_api_key:
            return
        if OpenAI is None:
            LOGGER.warning("GROQ_API_KEY is set but openai package is missing; AI rewriting disabled.")
            return
        self.client = OpenAI(
            api_key=config.groq_api_key,
            base_url="https://api.groq.com/openai/v1",
            max_retries=0,
            timeout=float(config.groq_timeout),
        )

    @property
    def enabled(self) -> bool:
        return self.client is not None and not self.disabled_reason

    def rewrite_plain(self, source_text: str) -> str:
        return self.recreate_plain("", source_text)

    def recreate_plain(self, title: str, source_text: str) -> str:
        heading = f"Title: {title}\n\n" if title else ""
        return self._rewrite(
            source_text,
            heading
            + "Recreate this update as 4-6 fresh Hinglish bullet points (each starting with -). "
            "Do not copy source wording. Keep facts, dates, numbers, and deadlines accurate. "
            "Do not include any URLs in your answer.",
            is_html=False,
            recreate=True,
        )

    def recreate_digest_html(self, title: str, source_text: str) -> str:
        return self._rewrite(
            source_text,
            f"Title: {title}\n\n"
            "Recreate this update as clean WordPress HTML with exactly these sections:\n"
            "<h1>title</h1>\n"
            '<section class="pa-summary"><h2>Quick Summary</h2><ul><li>3-5 recreated bullet points</li></ul></section>\n'
            '<section class="pa-details"><h2>Key Details</h2><p>2-4 short recreated paragraphs with facts</p></section>\n'
            "Do not copy source sentences. Do not include <a> links or indianaukrihelp references.",
            is_html=True,
            recreate=True,
        )

    def rewrite_html(self, source_html: str) -> str:
        return self._rewrite(
            source_html,
            "Return clean HTML for WordPress content. Preserve every href and src attribute.",
            is_html=True,
        )

    def _rewrite(
        self,
        source: str,
        instruction: str,
        is_html: bool,
        *,
        recreate: bool = False,
    ) -> str:
        if not self.enabled:
            return source
        try:
            response = self.client.chat.completions.create(
                model=self.config.groq_model,
                messages=[
                    {"role": "system", "content": AI_SYSTEM_PROMPT},
                    {"role": "user", "content": f"{instruction}\n\n{source[:8000]}"},
                ],
                temperature=0.45 if recreate else (0.25 if is_html else 0.4),
                timeout=float(self.config.groq_timeout),
            )
            result = response.choices[0].message.content or ""
            result = strip_markdown_fence(result)
            if not fact_safety_check(source, result, is_html=is_html, allow_missing_urls=recreate):
                LOGGER.warning("AI output failed fact-safety checks; using cleaned original content.")
                return source
            return result
        except Exception as exc:
            error = str(exc)
            lower_error = error.lower()
            if "429" in lower_error or "rate limit" in lower_error or "too many requests" in lower_error:
                self.disabled_reason = "rate limited"
                LOGGER.warning("AI rewriting is rate-limited; disabling AI rewriting for the rest of this run.")
                return source
            LOGGER.warning("AI rewriting failed; using cleaned original content. Error: %s", exc)
            return source


def strip_markdown_fence(value: str) -> str:
    value = (value or "").strip()
    value = re.sub(r"^```(?:html|text|txt)?\s*", "", value, flags=re.I)
    value = re.sub(r"\s*```$", "", value)
    return value.strip()


def count_html_assets(markup: str) -> tuple[int, int]:
    soup = make_soup(markup or "", "html.parser")
    images = len([img for img in soup.find_all("img") if img.get("src")])
    links = len([link for link in soup.find_all("a") if link.get("href")])
    return images, links


def fact_safety_check(
    original: str,
    candidate: str,
    is_html: bool,
    *,
    allow_missing_urls: bool = False,
) -> bool:
    if not candidate or not normalize_whitespace(strip_tags(candidate if is_html else candidate)):
        return False

    original_text = strip_tags(original) if is_html else normalize_whitespace(original)
    candidate_text = strip_tags(candidate) if is_html else normalize_whitespace(candidate)
    if len(original_text) > 180 and len(candidate_text) < max(80, int(len(original_text) * 0.35)):
        return False

    original_urls = set(extract_urls(original))
    candidate_urls = set(extract_urls(candidate))
    if original_urls and not allow_missing_urls:
        missing = original_urls - candidate_urls
        allowed_missing = max(1, len(original_urls) // 4)
        if len(missing) > allowed_missing:
            return False

    if is_html and not allow_missing_urls:
        original_images, original_links = count_html_assets(original)
        new_images, new_links = count_html_assets(candidate)
        if new_images < original_images:
            return False
        if new_links < max(0, original_links - max(1, original_links // 4)):
            return False
    return True


def guess_filename(source_url: str, content_type: str = "") -> str:
    parsed_path = urlparse(source_url).path
    filename = os.path.basename(parsed_path).strip() or f"file-{int(time.time() * 1000)}"
    filename = re.sub(r"[^A-Za-z0-9._-]+", "-", filename).strip("-")
    guessed_ext = mimetypes.guess_extension(normalize_mime(content_type)) or ""
    if guessed_ext == ".jpe":
        guessed_ext = ".jpg"
    if "." not in filename and guessed_ext:
        filename += guessed_ext
    if "." not in filename:
        filename += ".bin"
    return filename[:120]


def normalize_mime(content_type: str | None) -> str:
    return (content_type or "").split(";", 1)[0].strip().lower()


def sanitize_pdf_remove_links(pdf_bytes: bytes) -> bytes:
    if pikepdf is None:
        LOGGER.warning("pikepdf is not installed; PDF link sanitization skipped.")
        return pdf_bytes
    try:
        src = pikepdf.Pdf.open(io.BytesIO(pdf_bytes))
        for page in src.pages:
            annots = page.get("/Annots", None)
            if not annots:
                continue
            new_annots = []
            for annot in annots:
                try:
                    obj = annot.get_object()
                    if "/A" in obj:
                        del obj["/A"]
                    if "/AA" in obj:
                        del obj["/AA"]
                    if "/Dest" in obj:
                        del obj["/Dest"]
                    if obj.get("/Subtype", None) == pikepdf.Name("/Link"):
                        continue
                    new_annots.append(annot)
                except Exception:
                    continue
            if new_annots:
                page["/Annots"] = pikepdf.Array(new_annots)
            elif "/Annots" in page:
                del page["/Annots"]
        out = io.BytesIO()
        src.save(out)
        return out.getvalue()
    except Exception as exc:
        LOGGER.warning("PDF sanitization failed; sending original PDF bytes. Error: %s", exc)
        return pdf_bytes


def parse_feed(xml_data: str, feed_url: str) -> list[FeedItem]:
    soup = make_soup(xml_data, "xml")
    nodes = soup.find_all("item")
    if not nodes:
        nodes = soup.find_all("entry")

    items: list[FeedItem] = []
    for node in nodes:
        title_raw = node.title.get_text(" ", strip=True) if node.find("title") else "Educational Update"
        link = extract_feed_link(node, feed_url)
        guid = extract_feed_guid(node, link, title_raw)
        content_html = extract_feed_content_html(node)
        text = html_to_text_with_links(content_html, link or feed_url)
        title = remove_prefixes(strip_tags(title_raw)) or "Educational Update"
        text = remove_prefixes(text)
        if title and text and title.lower() not in text[:160].lower():
            text = normalize_whitespace(f"{title}\n\n{text}")
        source_url = link or first_non_spam_url(text) or feed_url
        enclosure_url, enclosure_type = extract_enclosure(node, feed_url)
        content_hash = sha256_text("|".join([title, text, content_html, source_url, enclosure_url]))
        items.append(
            FeedItem(
                guid=guid,
                title=title[:180],
                text=text,
                html_content=content_html,
                source_url=source_url,
                enclosure_url=enclosure_url,
                enclosure_type=enclosure_type,
                content_hash=content_hash,
            )
        )
    return items


def extract_feed_link(node: Any, feed_url: str) -> str:
    link_node = node.find("link")
    if not link_node:
        return ""
    if link_node.get("href"):
        return safe_url(link_node.get("href"), feed_url)
    return safe_url(link_node.get_text(" ", strip=True), feed_url)


def extract_feed_guid(node: Any, source_url: str, title: str) -> str:
    for tag_name in ("guid", "id"):
        tag = node.find(tag_name)
        if tag and tag.get_text(strip=True):
            return tag.get_text(strip=True)
    return source_url or sha256_text(title)


def extract_feed_content_html(node: Any) -> str:
    for tag_name in ("content:encoded", "encoded", "content", "description", "summary"):
        tag = node.find(tag_name)
        if tag and tag.get_text(strip=True):
            content = tag.decode_contents() if tag.contents else tag.get_text(" ", strip=True)
            return html.unescape(content)
    return ""


def extract_enclosure(node: Any, feed_url: str) -> tuple[str, str]:
    enclosure = node.find("enclosure")
    if enclosure and enclosure.get("url"):
        return safe_url(enclosure.get("url"), feed_url), normalize_mime(enclosure.get("type", ""))

    media = node.find("media:content") or node.find("content", attrs={"url": True})
    if media and media.get("url"):
        return safe_url(media.get("url"), feed_url), normalize_mime(media.get("type", ""))

    for link in node.find_all("link"):
        rel = " ".join(link.get("rel", []) if isinstance(link.get("rel"), list) else [str(link.get("rel", ""))])
        if "enclosure" in rel.lower() and link.get("href"):
            return safe_url(link.get("href"), feed_url), normalize_mime(link.get("type", ""))
    return "", ""


def enrich_item_media(item: FeedItem) -> None:
    if item.enclosure_url:
        return
    markup = item.html_content or ""
    if markup:
        soup = make_soup(markup, "html.parser")
        for img in soup.find_all("img"):
            source_url = image_candidate_from_tag(img, item.source_url or "")
            if looks_like_real_image(source_url):
                item.enclosure_url = source_url
                item.enclosure_type = normalize_mime(mimetypes.guess_type(source_url)[0] or "image/jpeg")
                return
    for url in extract_urls("\n".join([item.text or "", markup])):
        if looks_like_real_image(url):
            item.enclosure_url = url
            item.enclosure_type = normalize_mime(mimetypes.guess_type(url)[0] or "image/jpeg")
            return


def fetch_telegram_embed_content(source_url: str, session: requests.Session) -> tuple[str, str, str, str]:
    """Fetch text/html/image from a public t.me post embed page."""
    if not source_url:
        return "", "", "", ""
    lower_url = source_url.lower()
    if "t.me/" not in lower_url and "telegram.me/" not in lower_url:
        return "", "", "", ""
    embed_url = source_url.split("?")[0] + "?embed=1"
    try:
        response = request_with_flood_retry(
            session,
            "GET",
            embed_url,
            max_attempts=2,
            label=f"telegram_embed:{source_url[:80]}",
            headers=default_headers(embed_url),
            timeout=20,
            verify=True,
        )
        if response.status_code != 200 or not response.text:
            return "", "", "", ""
        soup = make_soup(response.text, "html.parser")
        text_node = soup.select_one(".tgme_widget_message_text")
        text = text_node.get_text("\n", strip=True) if text_node else ""
        html_content = str(text_node) if text_node else ""
        for img in soup.select(".tgme_widget_message_photo_wrap img, .tgme_widget_message_wrap img"):
            img_url = safe_url(img.get("src", ""), source_url)
            if looks_like_real_image(img_url):
                img_type = normalize_mime(mimetypes.guess_type(img_url)[0] or "image/jpeg")
                return text, html_content, img_url, img_type
        return text, html_content, "", ""
    except Exception as exc:
        LOGGER.warning("Telegram embed fetch failed for %s: %s", source_url, exc)
        return "", "", "", ""


def feed_item_from_catchup_row(row: sqlite3.Row, session: requests.Session | None = None) -> FeedItem:
    guid = row["guid"] or ""
    title = (row["title"] or "").strip()
    source_url = (row["source_url"] or guid).strip()
    text = title
    html_content = ""
    enclosure_url = ""
    enclosure_type = ""
    if session:
        embed_text, embed_html, img_url, img_type = fetch_telegram_embed_content(source_url, session)
        if embed_text:
            text = embed_text
        if embed_html:
            html_content = embed_html
        if img_url:
            enclosure_url = img_url
            enclosure_type = img_type
    return FeedItem(
        guid=guid,
        title=title or text[:200],
        text=text,
        html_content=html_content,
        source_url=source_url,
        enclosure_url=enclosure_url,
        enclosure_type=enclosure_type,
    )


def send_post_link_followup(telegram: "TelegramClient", channels: list[str], wp_link: str, delay: int = 0) -> None:
    if not wp_link or not urlparse(wp_link).path.strip("/"):
        return
    message = f"📌 Full Post: {wp_link}"
    for index, channel in enumerate(channels):
        if index and delay > 0:
            time.sleep(delay)
        try:
            telegram.send_text(channel, message)
        except Exception as exc:
            LOGGER.warning("Could not send WordPress follow-up to %s: %s", channel, exc)


def first_non_spam_url(text: str) -> str:
    for url in extract_urls(text):
        if not is_spam_url(url):
            return url
    return ""


def build_wordpress_content(item: FeedItem, ai: AIRewriter, important_links: list[LinkInfo]) -> str:
    raw_html = item.html_content or text_to_html(item.text)
    cleaned_html = sanitize_html_content(raw_html, item.source_url or "")
    cleaned_html = add_digest_heading(cleaned_html, item.title)
    cleaned_html = ai.rewrite_html(cleaned_html)
    pieces = [cleaned_html, source_block(item.source_url), important_links_block(important_links)]
    if item.enclosure_url and normalize_mime(item.enclosure_type).startswith("image/"):
        pieces.append(
            "<figure>"
            f'<img src="{html.escape(item.enclosure_url, quote=True)}" alt="{html.escape(item.title, quote=True)}" '
            'style="max-width:100%; height:auto;" loading="lazy" decoding="async">'
            "</figure>"
        )
    return "\n".join(piece for piece in pieces if piece)


def build_source_page_content(
    title: str,
    source_url: str,
    source_html: str,
    fallback_text: str,
    ai: AIRewriter,
    important_links: list[LinkInfo],
) -> str:
    raw_html = source_html or text_to_html(fallback_text)
    cleaned_html = sanitize_html_content(raw_html, source_url)
    cleaned_html = add_digest_heading(cleaned_html, title)
    cleaned_html = ai.rewrite_html(cleaned_html)
    pieces = [cleaned_html, source_block(source_url), important_links_block(important_links)]
    return "\n".join(piece for piece in pieces if piece)


def add_digest_heading(content_html: str, title: str) -> str:
    escaped_title = html.escape(title)
    return f"<h1>{escaped_title}</h1>\n{content_html}"


def sentence_candidates(text: str) -> list[str]:
    clean = remove_spam_urls_from_text(strip_tags(text))
    clean = re.sub(r"\[[^\]]{0,20}\]", "", clean)
    parts: list[str] = []
    for line in clean.splitlines():
        line = line.strip(" -\t")
        if not line or URL_RE.search(line) or len(line) < 8:
            continue
        subparts = re.split(r"(?<=[.!?])\s+", line)
        for part in subparts:
            part = normalize_whitespace(part).strip(" -")
            if 8 <= len(part) <= 240 and not line_has_spam(part):
                parts.append(part)
    output: list[str] = []
    seen: set[str] = set()
    for part in parts:
        key = re.sub(r"\W+", "", part.lower())[:80]
        if key and key not in seen:
            seen.add(key)
            output.append(part)
        if len(output) >= 8:
            break
    return output


def build_caption(
    title: str,
    content_text: str,
    fallback_text: str,
    wp_link: str,
    source_url: str,
    important_links: list[LinkInfo],
    config: Config,
    limit: int,
) -> str:
    title_key = re.sub(r"\W+", "", title.lower())
    points = sentence_candidates(content_text)
    points = [point for point in points if re.sub(r"\W+", "", point.lower()) != title_key]
    if len(points) < 3:
        for point in sentence_candidates(fallback_text):
            point_key = re.sub(r"\W+", "", point.lower())
            if point not in points and point_key != title_key:
                points.append(point)
            if len(points) >= 5:
                break
    points = points[:5]

    source = source_url or (important_links[0].href if important_links else "")
    fixed_tail: list[str] = []
    if wp_link:
        fixed_tail.append(f"Website: {wp_link}")
    if source:
        fixed_tail.append(f"Official/Source link: {source}")
    if config.follow_line_tg:
        fixed_tail.append(config.follow_line_tg)
    if config.follow_line_wa:
        fixed_tail.append(config.follow_line_wa)

    for point_count in range(min(5, len(points)), -1, -1):
        lines = [title.strip()[:180]]
        lines.extend(f"- {point}" for point in points[:point_count])
        if fixed_tail:
            lines.append("")
            lines.extend(fixed_tail)
        candidate = normalize_whitespace("\n".join(lines))
        if len(candidate) <= limit:
            return candidate

    minimal = normalize_whitespace("\n".join([title.strip()[:180], "", *fixed_tail]))
    return trim_preserving_urls(minimal, limit)


def trim_preserving_urls(text: str, limit: int) -> str:
    text = normalize_whitespace(text)
    if len(text) <= limit:
        return text

    lines = text.splitlines()
    output: list[str] = []
    current_len = 0
    suffix = "\n..."
    for line in lines:
        add_len = len(line) + (1 if output else 0)
        if current_len + add_len <= limit:
            output.append(line)
            current_len += add_len
            continue
        if URL_RE.search(line):
            continue
        remaining = limit - current_len - len(suffix) - (1 if output else 0)
        if remaining > 20:
            output.append(line[:remaining].rstrip() + "...")
        break
    trimmed = "\n".join(output).strip()
    return trimmed[:limit].rstrip()


class MirrorBot:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.session = build_session(config)
        self.state = StateStore(config.db_file)
        self.telegram = TelegramClient(config, self.session)
        self.wordpress = WordPressClient(config, self.session)
        self.ai = AIRewriter(config)
        self.started_at = time.monotonic()

    def close(self) -> None:
        self.state.close()

    def fetch_feed(self) -> str:
        LOGGER.info("Fetching feed: %s", self.config.feed_url)
        response = request_with_flood_retry(
            self.session,
            "GET",
            self.config.feed_url,
            max_attempts=self.config.flood_max_retries,
            label="feed_fetch",
            headers=default_headers(),
            timeout=30,
            verify=self.config.verify_ssl,
        )
        return response.text

    def run(self) -> None:
        LOGGER.info("Source RSS feed: %s", self.config.feed_url)
        LOGGER.info("Destination channel(s): %s", ", ".join(self.config.dest_channels))
        LOGGER.info(
            "WordPress target: %s | post_type=%s | skip_wordpress=%s",
            self.config.wp_url or "(disabled)",
            self.config.wp_post_type,
            self.config.skip_wordpress,
        )
        xml_data = self.fetch_feed()
        items = parse_feed(xml_data, self.config.feed_url)
        LOGGER.info("Parsed %s feed item(s).", len(items))

        if parse_bool(os.environ.get("WP_CATCHUP_ONLY"), False):
            self.catchup_wordpress_links(items)
            return

        if not self.config.skip_wordpress:
            self.catchup_wordpress_links(items)

        selected = self.select_items(items)
        if not selected:
            LOGGER.info("No pending items to process.")
            return
        LOGGER.info("Processing %s item(s).", len(selected))
        for item in selected:
            if self.time_budget_exceeded(reserve_seconds=45):
                LOGGER.warning("Stopping before next item to stay inside MAX_RUN_SECONDS=%s.", self.config.max_run_seconds)
                break
            self.process_one(item)
            if not self.time_budget_exceeded(reserve_seconds=2) and self.config.item_delay_seconds > 0:
                time.sleep(self.config.item_delay_seconds)

    def time_budget_exceeded(self, reserve_seconds: int = 0) -> bool:
        if self.config.max_run_seconds <= 0:
            return False
        elapsed = time.monotonic() - self.started_at
        return elapsed + reserve_seconds >= self.config.max_run_seconds

    def publish_wordpress_for_item(
        self,
        item: FeedItem,
        important_links: list[LinkInfo] | None = None,
        source_page_html: str = "",
        page_links: list[LinkInfo] | None = None,
    ) -> str:
        if not self.wordpress.ready or self.config.skip_wordpress:
            return ""
        if important_links is None:
            important_links = dedupe_links(
                [
                    *extract_important_links(item.html_content or item.text, item.source_url),
                    *extract_important_links(source_page_html, item.source_url),
                    *(page_links or []),
                ],
                limit=24,
            )
        try:
            wp_content = build_wordpress_content(item, self.ai, important_links)
            wp_link = self.wordpress.publish(item.title, wp_content, item.source_url or self.config.feed_url)
            if wp_link:
                self.state.set_wp_link(item.guid, wp_link)
            return wp_link or ""
        except Exception as exc:
            LOGGER.warning("WordPress publish failed for %s: %s", item.title[:80], exc)
            return ""

    def catchup_wordpress_links(self, feed_items: list[FeedItem]) -> None:
        if self.config.skip_wordpress or not self.wordpress.ready:
            return
        if not parse_bool(os.environ.get("WP_CATCHUP"), True):
            return

        by_guid = {item.guid: item for item in feed_items}
        pending_rows = self.state.list_published_without_wp_link(limit=12)
        if not pending_rows:
            return

        LOGGER.info("WordPress catch-up: %s published item(s) missing post links.", len(pending_rows))
        for row in pending_rows:
            if self.time_budget_exceeded(reserve_seconds=60):
                LOGGER.warning("Stopping WordPress catch-up to stay inside MAX_RUN_SECONDS.")
                break
            item = by_guid.get(row["guid"])
            if not item:
                item = feed_item_from_catchup_row(row, self.session)
                LOGGER.info("Catch-up using stored row (outside feed window): %s", row["guid"])
            item.text = remove_spam_urls_from_text(item.text)
            enrich_item_media(item)
            source_page_html, page_links = "", []
            if self.config.fetch_source_for_links and item.source_url:
                source_page_html, page_links = self.fetch_source_context(item.source_url)
            wp_link = self.publish_wordpress_for_item(
                item,
                source_page_html=source_page_html,
                page_links=page_links,
            )
            if wp_link:
                LOGGER.info("Catch-up published WordPress post: %s", wp_link)
                send_post_link_followup(
                    self.telegram,
                    self.config.dest_channels,
                    wp_link,
                    delay=self.config.item_delay_seconds,
                )
            elif not self.config.dry_run:
                LOGGER.warning("Catch-up could not create WordPress post for %s", row["guid"])

    def select_items(self, items: list[FeedItem]) -> list[FeedItem]:
        selected: list[FeedItem] = []
        for item in items:
            self.state.upsert_pending(item)
            row = self.state.get(item.guid)
            if row and row["status"] in {"published", "skipped"}:
                LOGGER.info("Skipping already %s item: %s", row["status"], item.title[:80])
                continue
            if row and row["status"] == "failed" and int(row["retries"] or 0) >= self.config.max_retries:
                reason = f"MAX_RETRIES exceeded ({self.config.max_retries})"
                LOGGER.warning("Skipping %s: %s", item.title[:80], reason)
                self.state.mark_skipped(item.guid, reason)
                continue
            selected.append(item)
            if len(selected) >= self.config.max_items_per_run:
                break
        return selected

    def process_one(self, item: FeedItem) -> None:
        LOGGER.info("Processing item: %s", item.title[:100])
        try:
            item.text = remove_spam_urls_from_text(item.text)
            skip_phrase = message_has_skip_phrase(item, self.config.skip_message_phrases)
            if skip_phrase:
                reason = f"Blocked phrase present: {skip_phrase}"
                self.state.mark_skipped(item.guid, reason)
                LOGGER.info("Item skipped: %s", reason)
                return
            if looks_like_ad_message(item.text):
                self.state.mark_skipped(item.guid, "Advertisement/promotional message")
                LOGGER.info("Item skipped as advertisement/promotional message: %s", item.title[:80])
                return
            if not item.text and not item.html_content:
                self.state.mark_skipped(item.guid, "No useful content after spam cleanup")
                LOGGER.warning("Item skipped after cleanup: %s", item.title[:80])
                return

            enrich_item_media(item)

            should_fetch_source = self.config.max_source_pages_per_item > 0 or self.config.fetch_source_for_links
            if should_fetch_source and item.source_url:
                source_page_html, page_links = self.fetch_source_context(item.source_url)
            else:
                source_page_html, page_links = "", []
            important_links = dedupe_links(
                [
                    *extract_important_links(item.html_content or item.text, item.source_url),
                    *extract_important_links(source_page_html, item.source_url),
                    *page_links,
                ],
                limit=24,
            )

            row = self.state.get(item.guid)
            wp_link = row["wp_link"] if row and row["wp_link"] else ""

            try:
                source_replacements = self.create_source_pages(
                    item,
                    existing_wp_link=wp_link,
                    initial_source_html=source_page_html,
                    initial_page_links=page_links,
                )
            except Exception as exc:
                source_replacements = {}
                LOGGER.warning("WordPress source-page creation failed; continuing without replacement links: %s", exc)
            if source_replacements:
                item.text = apply_link_replacements_text(item.text, source_replacements)
                item.html_content = apply_link_replacements_html(item.html_content, source_replacements, item.source_url)
                wp_link = wp_link or next(iter(source_replacements.values()))
                if wp_link:
                    self.state.set_wp_link(item.guid, wp_link)

            if self.wordpress.ready and not self.config.skip_wordpress and not wp_link:
                wp_link = self.publish_wordpress_for_item(
                    item,
                    important_links=important_links,
                    source_page_html=source_page_html,
                    page_links=page_links,
                )
                if not wp_link and not self.config.dry_run:
                    LOGGER.warning("WordPress publish did not return a full post link.")

            caption_source_url = self.caption_source_url(item.source_url, source_replacements)
            if self.config.dry_run:
                LOGGER.info("[DRY_RUN] Would dispatch Telegram for: %s", item.title[:80])
            else:
                self.dispatch_telegram(item, wp_link, important_links, caption_source_url)

            if self.config.dry_run:
                LOGGER.info("[DRY_RUN] Processed item without changing published/skipped state: %s", item.title[:80])
                return

            self.state.mark_published(item.guid, wp_link)
            LOGGER.info("Published item: %s", item.title[:100])
        except Exception as exc:
            error = str(exc)
            self.state.mark_failed(item.guid, error)
            LOGGER.error("Item failed: %s | %s", item.title[:100], error)

    def source_page_urls_from_item(self, item: FeedItem) -> list[str]:
        urls = [item.source_url, *extract_urls(item.text), *extract_urls(strip_tags(item.html_content))]
        output: list[str] = []
        seen: set[str] = set()
        for url in urls:
            url = safe_url(url, item.source_url or self.config.feed_url)
            if not url or not host_matches(url, self.config.source_page_hosts):
                continue
            key = canonical_url(url)
            if key in seen:
                continue
            seen.add(key)
            output.append(url)
        return output

    def create_source_pages(
        self,
        item: FeedItem,
        existing_wp_link: str = "",
        initial_source_html: str = "",
        initial_page_links: list[LinkInfo] | None = None,
    ) -> dict[str, str]:
        source_urls = self.source_page_urls_from_item(item)
        if self.config.max_source_pages_per_item <= 0:
            return {}
        source_urls = source_urls[: self.config.max_source_pages_per_item]
        if not source_urls:
            return {}
        if not self.wordpress.ready:
            raise RuntimeError("WordPress credentials are required to replace source-page links")
        if existing_wp_link and len(source_urls) == 1:
            return {canonical_url(source_urls[0]): existing_wp_link}

        replacements: dict[str, str] = {}
        for source_url in source_urls:
            if self.time_budget_exceeded(reserve_seconds=35):
                LOGGER.warning("Stopping source-page creation to stay inside MAX_RUN_SECONDS=%s.", self.config.max_run_seconds)
                break
            LOGGER.info("Creating transparent source page for %s", source_url)
            if canonical_url(source_url) == canonical_url(item.source_url):
                source_html = initial_source_html
                page_links = initial_page_links or []
            else:
                source_html, page_links = self.fetch_source_context(source_url)
            important_links = dedupe_links(
                [
                    *extract_important_links(source_html, source_url),
                    *extract_important_links(item.html_content or item.text, item.source_url),
                    *page_links,
                ],
                limit=24,
            )
            content = build_source_page_content(item.title, source_url, source_html, item.text, self.ai, important_links)
            wp_link = self.wordpress.publish(item.title, content, source_url)
            if not wp_link and not self.config.dry_run:
                raise RuntimeError(f"WordPress did not return a link for source page: {source_url}")
            if wp_link:
                replacements[canonical_url(source_url)] = wp_link
        return replacements

    def caption_source_url(self, source_url: str, source_replacements: dict[str, str]) -> str:
        if source_replacements and host_matches(source_url, self.config.source_page_hosts):
            return ""
        return source_url

    def fetch_source_context(self, source_url: str) -> tuple[str, list[LinkInfo]]:
        if not source_url or is_spam_url(source_url):
            return "", []
        lower_url = source_url.lower()
        if "t.me/" in lower_url or "telegram.me/" in lower_url:
            return "", []
        try:
            response = request_with_flood_retry(
                self.session,
                "GET",
                source_url,
                max_attempts=self.config.flood_max_retries,
                label=f"source_fetch:{source_url[:80]}",
                headers=default_headers(source_url),
                timeout=30,
                verify=self.config.verify_ssl,
            )
            if response.status_code != 200:
                return "", []
            if "text/html" not in normalize_mime(response.headers.get("Content-Type", "text/html")):
                return "", []
            soup = make_soup(response.text, "html.parser")
            normalize_links(soup, source_url)
            links = extract_important_links(str(soup), source_url)
            clean_layout_noise(soup)
            article = select_article(soup)
            if not article:
                return "", links
            normalize_links(article, source_url)
            return sanitize_html_content(str(article), source_url), links
        except Exception as exc:
            LOGGER.warning("Source context fetch failed for %s: %s", source_url, exc)
            return "", []

    def dispatch_telegram(
        self,
        item: FeedItem,
        wp_link: str,
        important_links: list[LinkInfo],
        caption_source_url: str,
    ) -> None:
        ctype = normalize_mime(item.enclosure_type)
        clean_text = remove_spam_urls_from_text(item.text)
        rewritten_text = self.ai.recreate_plain(item.title, clean_text)
        media_caption = build_caption(
            item.title,
            rewritten_text,
            clean_text,
            wp_link,
            caption_source_url,
            important_links,
            self.config,
            900,
        )
        text_caption = build_caption(
            item.title,
            rewritten_text,
            clean_text,
            wp_link,
            caption_source_url,
            important_links,
            self.config,
            3900,
        )

        if item.enclosure_url and ctype == "application/pdf":
            self.send_pdf_item(item, media_caption, text_caption)
            return

        if item.enclosure_url and (ctype.startswith("image/") or looks_like_real_image(item.enclosure_url)):
            self.send_image_item(item, media_caption, text_caption)
            return

        for index, channel in enumerate(self.config.dest_channels):
            if index and self.config.item_delay_seconds > 0:
                time.sleep(self.config.item_delay_seconds)
            self.telegram.send_text(channel, text_caption)

    def send_pdf_item(self, item: FeedItem, media_caption: str, fallback_text: str) -> None:
        try:
            response = self.session.get(
                item.enclosure_url,
                headers=default_headers(self.config.feed_url),
                timeout=60,
                verify=self.config.verify_ssl,
            )
            response.raise_for_status()
            content_type = normalize_mime(response.headers.get("Content-Type", "")) or normalize_mime(item.enclosure_type)
            if content_type != "application/pdf":
                raise ValueError(f"Enclosure MIME is not application/pdf: {content_type or 'unknown'}")
            safe_pdf = sanitize_pdf_remove_links(response.content)
            filename = guess_filename(item.enclosure_url, "application/pdf")
            if not filename.lower().endswith(".pdf"):
                filename += ".pdf"
        except Exception as exc:
            LOGGER.warning("PDF download/sanitization failed; falling back to text message. Error: %s", exc)
            for channel in self.config.dest_channels:
                self.telegram.send_text(channel, fallback_text)
            return

        for channel in self.config.dest_channels:
            try:
                self.telegram.send_document(channel, safe_pdf, filename, media_caption)
            except Exception as exc:
                LOGGER.warning("PDF send failed for %s; falling back to text. Error: %s", channel, exc)
                self.telegram.send_text(channel, fallback_text)

    def send_image_item(self, item: FeedItem, media_caption: str, fallback_text: str) -> None:
        try:
            response = self.session.get(
                item.enclosure_url,
                headers=default_headers(self.config.feed_url),
                timeout=60,
                verify=self.config.verify_ssl,
            )
            response.raise_for_status()
            content_type = normalize_mime(response.headers.get("Content-Type", "")) or normalize_mime(item.enclosure_type)
            if not content_type.startswith("image/"):
                guessed = mimetypes.guess_type(item.enclosure_url)[0] or ""
                if not guessed.startswith("image/"):
                    raise ValueError(f"Enclosure MIME is not an image: {content_type or 'unknown'}")
                content_type = guessed
        except Exception as exc:
            LOGGER.warning("Image download/validation failed; falling back to text message. Error: %s", exc)
            for channel in self.config.dest_channels:
                self.telegram.send_text(channel, fallback_text)
            return

        for index, channel in enumerate(self.config.dest_channels):
            if index and self.config.item_delay_seconds > 0:
                time.sleep(self.config.item_delay_seconds)
            try:
                self.telegram.send_photo(channel, response.content, media_caption, content_type)
            except Exception as exc:
                LOGGER.warning("Photo send failed for %s; falling back to text. Error: %s", channel, exc)
                self.telegram.send_text(channel, fallback_text)


def main() -> None:
    setup_logging()
    bot: MirrorBot | None = None
    try:
        config = Config.from_env()
        if BeautifulSoup is None:
            raise RuntimeError("beautifulsoup4 is required. Install it with: pip install beautifulsoup4")
        bot = MirrorBot(config)
        LOGGER.info("Bot started. DRY_RUN=%s DB_FILE=%s", config.dry_run, config.db_file)
        bot.run()
        LOGGER.info("Bot finished.")
    except Exception as exc:
        LOGGER.critical("Critical bot error: %s", exc)
        try:
            if bot is not None:
                bot.telegram.send_admin_critical(str(exc))
            else:
                config = Config.from_env()
                telegram = TelegramClient(config, build_session(config))
                telegram.send_admin_critical(str(exc))
        except Exception:
            pass
    finally:
        if bot is not None:
            bot.close()


if __name__ == "__main__":
    main()
