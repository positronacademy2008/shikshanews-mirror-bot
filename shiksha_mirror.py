"""Shiksha News Rajasthan mirror bot — indianaukrihelp.com pages to positronacademy.in."""
from __future__ import annotations

import logging
import mimetypes
import os
import re
import time
from urllib.parse import urlparse

import run_bot  # noqa: F401 — mirror sanitization + WP page builders

import bot

LOGGER = logging.getLogger("shiksha_mirror")

MEDIA_URL_HINTS = (
    "telesco.pe",
    "cdn-telegram",
    "cdn4.cdn-telegram.org",
    "tg.i-c-a.su",
    "telegram.org/file",
)

FEED_CHANNEL_LINE_HINTS = (
    "join telegram",
    "join our telegram",
    "follow telegram",
    "telegram channel",
    "join channel",
    "follow channel",
    "subscribe channel",
)

_original_send_pdf_item = bot.MirrorBot.send_pdf_item


def _title_key(value: str) -> str:
    return re.sub(r"\W+", "", bot.normalize_whitespace(value).lower())[:100]


def _is_pdf_url(url: str) -> bool:
    return urlparse(bot.clean_url(url)).path.lower().endswith(".pdf")


def _is_media_cdn_url(url: str) -> bool:
    lower = bot.clean_url(url).lower()
    return any(hint in lower for hint in MEDIA_URL_HINTS)


def _is_telegram_channel_url(url: str) -> bool:
    lower = bot.clean_url(url).lower()
    return "t.me/" in lower or "telegram.me/" in lower


def _is_media_attachment_url(url: str, enclosure_url: str = "") -> bool:
    if not url:
        return False
    clean = bot.clean_url(url)
    if not _is_media_cdn_url(clean):
        return False
    if enclosure_url and clean == bot.clean_url(enclosure_url):
        return True
    if _is_pdf_url(clean):
        return True
    return bot.looks_like_real_image(clean)


def _strip_media_urls(text: str, enclosure_url: str = "") -> str:
    def replace(match: re.Match[str]) -> str:
        url = bot.clean_url(match.group(1))
        return "" if _is_media_attachment_url(url, enclosure_url) else url

    cleaned = bot.URL_RE.sub(replace, text or "")

    def paren_replace(match: re.Match[str]) -> str:
        url = bot.clean_url(match.group(1))
        return "" if _is_media_attachment_url(url, enclosure_url) else match.group(0)

    cleaned = re.sub(r"\(\s*(https?://[^\s)]+)\s*\)", paren_replace, cleaned)
    cleaned = re.sub(r"\(\s*\)", "", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return bot.normalize_whitespace(cleaned)


def _strip_feed_channel_refs(text: str) -> str:
    def url_replace(match: re.Match[str]) -> str:
        url = bot.clean_url(match.group(1))
        return "" if _is_telegram_channel_url(url) else url

    cleaned = bot.URL_RE.sub(url_replace, text or "")
    cleaned = re.sub(r"@[A-Za-z][A-Za-z0-9_]{2,}", "", cleaned)

    kept: list[str] = []
    for raw_line in cleaned.splitlines():
        line = bot.normalize_whitespace(raw_line)
        if not line:
            if kept and kept[-1] != "":
                kept.append("")
            continue
        lower = line.lower()
        if any(hint in lower for hint in FEED_CHANNEL_LINE_HINTS):
            continue
        if _is_telegram_channel_url(line):
            continue
        kept.append(line)
    return bot.normalize_whitespace("\n".join(kept))


def _dedupe_title_lines(text: str, title: str) -> str:
    display_title = run_bot.clean_title(title, "", text)
    key = _title_key(display_title)
    if not key:
        return bot.normalize_whitespace(text)

    seen = False
    kept: list[str] = []
    for raw_line in (text or "").splitlines():
        line = bot.normalize_whitespace(raw_line)
        if not line:
            if kept and kept[-1] != "":
                kept.append("")
            continue
        if re.search(r"\[\s*\.{3}\s*\]", line):
            continue
        line_key = _title_key(re.sub(r"\[\s*\.{3}\s*\]", "", line))
        if key and (line_key == key or (len(key) >= 24 and line_key.startswith(key[:24]))):
            if seen:
                continue
            seen = True
        kept.append(raw_line.strip())
    return bot.normalize_whitespace("\n".join(kept))


def _normalize_media_enclosure(item: bot.FeedItem) -> None:
    """Keep Telegram/RSS CDN photos and PDFs; drop article/site URLs mistaken as media."""
    saved_url, saved_type = item.enclosure_url, item.enclosure_type
    if (
        saved_url
        and _is_media_cdn_url(saved_url)
        and (_is_pdf_url(saved_url) or bot.normalize_mime(saved_type) == "application/pdf")
    ):
        item.enclosure_url = saved_url
        item.enclosure_type = "application/pdf"
        return

    item.enclosure_url, item.enclosure_type = "", ""
    bot.enrich_item_media(item)
    if not item.enclosure_url:
        item.enclosure_url, item.enclosure_type = saved_url, saved_type
    url = item.enclosure_url or ""
    if not url or not _is_media_cdn_url(url):
        item.enclosure_url = ""
        item.enclosure_type = ""
        return

    ctype = bot.normalize_mime(item.enclosure_type)
    if _is_pdf_url(url) or ctype == "application/pdf":
        item.enclosure_type = "application/pdf"
        return

    guessed = mimetypes.guess_type(url)[0] or ""
    if ctype.startswith("image/") or guessed.startswith("image/") or bot.looks_like_real_image(url):
        item.enclosure_type = bot.normalize_mime(guessed or "image/jpeg")
        return

    item.enclosure_url = ""
    item.enclosure_type = ""


def _has_valid_pdf(item: bot.FeedItem) -> bool:
    url = item.enclosure_url or ""
    if not url or not _is_media_cdn_url(url):
        return False
    ctype = bot.normalize_mime(item.enclosure_type)
    return ctype == "application/pdf" or _is_pdf_url(url)


def _has_valid_photo(item: bot.FeedItem) -> bool:
    url = item.enclosure_url or ""
    if not url or not _is_media_cdn_url(url) or _is_pdf_url(url):
        return False
    ctype = bot.normalize_mime(item.enclosure_type)
    guessed = mimetypes.guess_type(url)[0] or ""
    return ctype.startswith("image/") or guessed.startswith("image/")


def _ensure_wp_link_in_text(text: str, replacements: dict[str, str]) -> str:
    wp_links = [value for value in replacements.values() if value and "positronacademy.in" in value]
    if not wp_links:
        return text
    primary = wp_links[0]
    if primary in text:
        return text
    return bot.normalize_whitespace(f"{text}\n\n{primary}")


def _has_mirror_targets(item: bot.FeedItem, config: bot.Config) -> bool:
    urls = [
        item.source_url,
        *bot.extract_urls(item.text or ""),
        *bot.extract_urls(bot.strip_tags(item.html_content or "")),
    ]
    for url in urls:
        url = bot.safe_url(url, item.source_url or config.feed_url)
        if url and bot.host_matches(url, config.source_page_hosts):
            return True
    return False


def _should_process_item(item: bot.FeedItem, config: bot.Config) -> bool:
    return _has_mirror_targets(item, config) or _has_valid_photo(item) or _has_valid_pdf(item)


def _html_to_telegram_text(item: bot.FeedItem) -> str:
    markup = item.html_content or ""
    if not markup:
        return ""
    soup = bot.make_soup(markup, "html.parser")
    for img in soup.find_all("img"):
        src = bot.safe_url(img.get("src", ""), item.source_url)
        if _is_media_attachment_url(src, item.enclosure_url):
            img.decompose()
    for link in soup.find_all("a", href=True):
        href = bot.safe_url(link.get("href", ""), item.source_url)
        if _is_media_attachment_url(href, item.enclosure_url) or _is_telegram_channel_url(href):
            link.decompose()
    return bot.html_to_text_with_links(str(soup), item.source_url)


def _build_outbound_text(item: bot.FeedItem) -> str:
    text = bot.remove_spam_urls_from_text(item.text or "")
    if not text and item.html_content:
        text = _html_to_telegram_text(item)
    text = _dedupe_title_lines(text, item.title)
    text = _strip_feed_channel_refs(text)
    text = _strip_media_urls(text, item.enclosure_url)
    return bot.normalize_whitespace(text)


def _notify_admin(telegram: bot.TelegramClient, config: bot.Config, message: str) -> None:
    if not config.admin_chat_id:
        return
    try:
        telegram.send_text(config.admin_chat_id, message[:3900], disable_preview=False)
    except Exception as exc:
        LOGGER.warning("Admin notification failed: %s", exc)


def send_pdf_item(self: bot.MirrorBot, item: bot.FeedItem, media_caption: str, fallback_text: str) -> None:
    media_caption = _strip_feed_channel_refs(_strip_media_urls(media_caption, item.enclosure_url))
    fallback_text = _strip_feed_channel_refs(_strip_media_urls(fallback_text, item.enclosure_url))
    _original_send_pdf_item(self, item, media_caption, fallback_text)


def dispatch_replaced_message(
    self: bot.MirrorBot,
    item: bot.FeedItem,
    replacements: dict[str, str],
) -> None:
    """Send feed message to test channel with indianaukrihelp links replaced by our WP URLs."""
    text = _build_outbound_text(item)
    if replacements:
        text = bot.apply_link_replacements_text(text, replacements)
    text = _ensure_wp_link_in_text(text, replacements)
    text = _strip_feed_channel_refs(_strip_media_urls(text, item.enclosure_url))
    if not text:
        text = run_bot.clean_title(item.title, item.source_url or "", item.text or "")

    has_pdf = _has_valid_pdf(item)
    has_photo = _has_valid_photo(item)

    if has_pdf:
        caption = text[:900]
        self.send_pdf_item(item, caption, text)
        return

    if has_photo:
        caption = text[:900]
        self.send_image_item(item, caption, text)
        return

    for index, channel in enumerate(self.config.dest_channels):
        if index and self.config.item_delay_seconds > 0:
            time.sleep(self.config.item_delay_seconds)
        self.telegram.send_text(channel, text)


def process_mirror_item(self: bot.MirrorBot, item: bot.FeedItem) -> None:
    LOGGER.info("Processing feed item: %s", item.title[:100])
    try:
        item.text = bot.remove_spam_urls_from_text(item.text)
        item.text = _strip_feed_channel_refs(item.text)
        item.text = _dedupe_title_lines(item.text, item.title)
        if bot.message_has_skip_phrase(item, self.config.skip_message_phrases):
            self.state.mark_skipped(item.guid, "Blocked phrase present")
            return
        if bot.looks_like_ad_message(item.text):
            self.state.mark_skipped(item.guid, "Advertisement/promotional message")
            return

        _normalize_media_enclosure(item)
        media_enclosure_url = item.enclosure_url
        media_enclosure_type = item.enclosure_type
        has_targets = _has_mirror_targets(item, self.config)
        has_media = _has_valid_photo(item) or _has_valid_pdf(item)

        if not has_targets and not has_media:
            self.state.mark_skipped(item.guid, "No mirror target or media attachment")
            LOGGER.info("Skipped (no mirror target/media): %s", item.title[:80])
            return

        source_page_html, page_links = "", []
        if has_targets and item.source_url and not bot.host_matches(item.source_url, self.config.source_page_hosts):
            source_page_html, page_links = self.fetch_source_context(item.source_url)

        row = self.state.get(item.guid)
        wp_link = row["wp_link"] if row and row["wp_link"] else ""
        source_replacements: dict[str, str] = {}

        if has_targets:
            source_replacements = self.create_source_pages(
                item,
                existing_wp_link=wp_link,
                initial_source_html=source_page_html,
                initial_page_links=page_links,
            )
            if not source_replacements:
                raise RuntimeError("WordPress mirror did not return replacement links")
            item.text = bot.apply_link_replacements_text(item.text, source_replacements)
            item.html_content = bot.apply_link_replacements_html(
                item.html_content, source_replacements, item.source_url
            )
            wp_link = wp_link or next(iter(source_replacements.values()))
            if wp_link:
                self.state.set_wp_link(item.guid, wp_link)

        item.text = _dedupe_title_lines(item.text, item.title)
        if source_replacements:
            item.text = _ensure_wp_link_in_text(item.text, source_replacements)
        item.enclosure_url = media_enclosure_url
        item.enclosure_type = media_enclosure_type

        if self.config.dry_run:
            LOGGER.info("[DRY_RUN] Would send to %s: %s", self.config.dest_channels, item.title[:80])
            return

        dispatch_replaced_message(self, item, source_replacements)
        self.state.mark_published(item.guid, wp_link)

        if source_replacements:
            mirror_lines = "\n".join(f"• {old} → {new}" for old, new in list(source_replacements.items())[:4])
            admin_message = (
                "✅ Mirror webpage published\n"
                f"Title: {item.title[:200]}\n"
                f"Webpage: {wp_link}\n"
                f"Sent to: {', '.join(self.config.dest_channels)}\n"
                f"Replacements:\n{mirror_lines}"
            )
        else:
            media_kind = "PDF" if _has_valid_pdf(item) else "Photo"
            admin_message = (
                f"✅ {media_kind} forwarded (no indianaukrihelp link)\n"
                f"Title: {item.title[:200]}\n"
                f"Sent to: {', '.join(self.config.dest_channels)}"
            )
        _notify_admin(self.telegram, self.config, admin_message)
        LOGGER.info("Mirrored and sent: %s", item.title[:100])
    except Exception as exc:
        error = str(exc)
        self.state.mark_failed(item.guid, error)
        LOGGER.error("Mirror failed: %s | %s", item.title[:100], error)
        _notify_admin(
            self.telegram,
            self.config,
            f"❌ Mirror failed\nTitle: {item.title[:200]}\nError: {error[:1500]}",
        )


def patch_mirror_bot() -> None:
    bot.MirrorBot.process_one = process_mirror_item
    bot.MirrorBot.send_pdf_item = send_pdf_item


def main() -> None:
    os.environ.setdefault("FEED_URL", "https://tg.i-c-a.su/rss/shikshanewsrajasthan")
    os.environ.setdefault("DEST_CHANNEL", "@testsourcechannelA")
    os.environ.setdefault("SOURCE_PAGE_HOSTS", "indianaukrihelp.com")
    os.environ.setdefault("MAX_SOURCE_PAGES_PER_ITEM", "3")
    os.environ.setdefault("PAGE_BUILD_MODE", "mirror")
    os.environ.setdefault("SKIP_WORDPRESS", "false")
    os.environ.setdefault("WP_URL", "https://positronacademy.in")
    os.environ.setdefault("WP_POST_TYPE", "pages")
    os.environ.setdefault("BRAND_IMAGES", "false")
    os.environ.setdefault("WEB_FOLLOW_LINE", "")
    os.environ.setdefault("FOLLOW_LINE", "")
    os.environ.setdefault("FOLLOW_LINE_TG", "")
    os.environ.setdefault("FOLLOW_LINE_WA", "")
    patch_mirror_bot()
    bot.main()


if __name__ == "__main__":
    main()