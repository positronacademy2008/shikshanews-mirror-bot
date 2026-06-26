"""Shiksha News Rajasthan mirror bot — indianaukrihelp.com pages to positronacademy.in."""
from __future__ import annotations

import logging
import mimetypes
import os
import re
import time

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


def _title_key(value: str) -> str:
    return re.sub(r"\W+", "", bot.normalize_whitespace(value).lower())[:100]


def _is_media_url(url: str, enclosure_url: str = "") -> bool:
    if not url:
        return False
    clean = bot.clean_url(url)
    if enclosure_url and clean == bot.clean_url(enclosure_url):
        return True
    lower = clean.lower()
    if bot.looks_like_real_image(clean):
        return True
    return any(hint in lower for hint in MEDIA_URL_HINTS)


def _strip_media_urls(text: str, enclosure_url: str = "") -> str:
    def replace(match: re.Match[str]) -> str:
        url = bot.clean_url(match.group(1))
        return "" if _is_media_url(url, enclosure_url) else url

    cleaned = bot.URL_RE.sub(replace, text or "")
    cleaned = re.sub(r"\(\s*\)", "", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return bot.normalize_whitespace(cleaned)


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
        line_key = _title_key(line)
        if key and (line_key == key or (len(key) >= 24 and line_key.startswith(key[:24]))):
            if seen:
                continue
            seen = True
        kept.append(raw_line.strip())
    return bot.normalize_whitespace("\n".join(kept))


def _normalize_photo_enclosure(item: bot.FeedItem) -> None:
    """Ensure photo posts use a real image URL + mime before Telegram send."""
    if item.enclosure_url and bot.looks_like_real_image(item.enclosure_url):
        guessed = mimetypes.guess_type(item.enclosure_url)[0] or ""
        if guessed.startswith("image/"):
            item.enclosure_type = bot.normalize_mime(guessed)
            return
    bot.enrich_item_media(item)
    if item.enclosure_url and bot.looks_like_real_image(item.enclosure_url):
        guessed = mimetypes.guess_type(item.enclosure_url)[0] or "image/jpeg"
        item.enclosure_type = bot.normalize_mime(guessed)


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


def _html_to_telegram_text(item: bot.FeedItem) -> str:
    markup = item.html_content or ""
    if not markup:
        return ""
    soup = bot.make_soup(markup, "html.parser")
    for img in soup.find_all("img"):
        src = bot.safe_url(img.get("src", ""), item.source_url)
        if _is_media_url(src, item.enclosure_url):
            img.decompose()
    for link in soup.find_all("a", href=True):
        href = bot.safe_url(link.get("href", ""), item.source_url)
        if _is_media_url(href, item.enclosure_url):
            link.decompose()
    return bot.html_to_text_with_links(str(soup), item.source_url)


def _build_outbound_text(item: bot.FeedItem, *, for_photo: bool = False) -> str:
    text = bot.remove_spam_urls_from_text(item.text or "")
    if not text and item.html_content:
        text = _html_to_telegram_text(item)
    text = _dedupe_title_lines(text, item.title)
    text = _strip_media_urls(text, item.enclosure_url)
    return bot.normalize_whitespace(text)


def _notify_admin(telegram: bot.TelegramClient, config: bot.Config, message: str) -> None:
    if not config.admin_chat_id:
        return
    try:
        telegram.send_text(config.admin_chat_id, message[:3900], disable_preview=False)
    except Exception as exc:
        LOGGER.warning("Admin notification failed: %s", exc)


def dispatch_replaced_message(
    self: bot.MirrorBot,
    item: bot.FeedItem,
    replacements: dict[str, str],
) -> None:
    """Send feed message to test channel with indianaukrihelp links replaced by our WP URLs."""
    _normalize_photo_enclosure(item)
    ctype = bot.normalize_mime(item.enclosure_type)
    has_photo = bool(
        item.enclosure_url
        and (ctype.startswith("image/") or bot.looks_like_real_image(item.enclosure_url))
    )
    text = _build_outbound_text(item, for_photo=has_photo)
    if replacements:
        text = bot.apply_link_replacements_text(text, replacements)
    if not text:
        text = run_bot.clean_title(item.title, item.source_url or "", item.text)

    for index, channel in enumerate(self.config.dest_channels):
        if index and self.config.item_delay_seconds > 0:
            time.sleep(self.config.item_delay_seconds)
        if item.enclosure_url and ctype == "application/pdf":
            self.send_pdf_item(item, text[:900], text)
            continue
        if has_photo:
            caption = _strip_media_urls(text, item.enclosure_url)
            self.send_image_item(item, caption[:900], caption)
            continue
        self.telegram.send_text(channel, text)


def process_mirror_item(self: bot.MirrorBot, item: bot.FeedItem) -> None:
    LOGGER.info("Processing feed item: %s", item.title[:100])
    try:
        item.text = bot.remove_spam_urls_from_text(item.text)
        item.text = _dedupe_title_lines(item.text, item.title)
        if bot.message_has_skip_phrase(item, self.config.skip_message_phrases):
            self.state.mark_skipped(item.guid, "Blocked phrase present")
            return
        if bot.looks_like_ad_message(item.text):
            self.state.mark_skipped(item.guid, "Advertisement/promotional message")
            return

        if not _has_mirror_targets(item, self.config):
            self.state.mark_skipped(item.guid, "No indianaukrihelp.com link in message")
            LOGGER.info("Skipped (no mirror target): %s", item.title[:80])
            return

        _normalize_photo_enclosure(item)
        source_page_html, page_links = "", []
        if item.source_url and not bot.host_matches(item.source_url, self.config.source_page_hosts):
            source_page_html, page_links = self.fetch_source_context(item.source_url)

        row = self.state.get(item.guid)
        wp_link = row["wp_link"] if row and row["wp_link"] else ""

        source_replacements = self.create_source_pages(
            item,
            existing_wp_link=wp_link,
            initial_source_html=source_page_html,
            initial_page_links=page_links,
        )
        if not source_replacements:
            raise RuntimeError("WordPress mirror did not return replacement links")

        item.text = bot.apply_link_replacements_text(item.text, source_replacements)
        item.text = _dedupe_title_lines(item.text, item.title)
        item.html_content = bot.apply_link_replacements_html(
            item.html_content, source_replacements, item.source_url
        )
        wp_link = wp_link or next(iter(source_replacements.values()))
        if wp_link:
            self.state.set_wp_link(item.guid, wp_link)

        if self.config.dry_run:
            LOGGER.info("[DRY_RUN] Would send to %s: %s", self.config.dest_channels, item.title[:80])
            return

        dispatch_replaced_message(self, item, source_replacements)
        self.state.mark_published(item.guid, wp_link)

        mirror_lines = "\n".join(f"• {old} → {new}" for old, new in list(source_replacements.items())[:4])
        _notify_admin(
            self.telegram,
            self.config,
            (
                "✅ Mirror webpage published\n"
                f"Source feed: @shikshanewsrajasthan\n"
                f"Title: {item.title[:200]}\n"
                f"Webpage: {wp_link}\n"
                f"Sent to: {', '.join(self.config.dest_channels)}\n"
                f"Replacements:\n{mirror_lines}"
            ),
        )
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