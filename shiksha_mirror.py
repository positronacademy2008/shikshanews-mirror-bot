"""Shiksha News Rajasthan mirror bot — indianaukrihelp.com pages to positronacademy.in."""
from __future__ import annotations

import logging
import os
import time

import run_bot  # noqa: F401 — mirror sanitization + WP page builders

import bot

LOGGER = logging.getLogger("shiksha_mirror")


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


def _build_outbound_text(item: bot.FeedItem) -> str:
    text = bot.remove_spam_urls_from_text(item.text or "")
    if not text and item.html_content:
        text = bot.html_to_text_with_links(item.html_content, item.source_url)
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
    text = _build_outbound_text(item)
    if replacements:
        text = bot.apply_link_replacements_text(text, replacements)
    if not text:
        text = run_bot.clean_title(item.title, item.source_url or "", item.text)

    ctype = bot.normalize_mime(item.enclosure_type)
    for index, channel in enumerate(self.config.dest_channels):
        if index and self.config.item_delay_seconds > 0:
            time.sleep(self.config.item_delay_seconds)
        if item.enclosure_url and ctype == "application/pdf":
            self.send_pdf_item(item, text[:900], text)
            continue
        if item.enclosure_url and (ctype.startswith("image/") or bot.looks_like_real_image(item.enclosure_url)):
            self.send_image_item(item, text[:900], text)
            continue
        self.telegram.send_text(channel, text)


def process_mirror_item(self: bot.MirrorBot, item: bot.FeedItem) -> None:
    LOGGER.info("Processing feed item: %s", item.title[:100])
    try:
        item.text = bot.remove_spam_urls_from_text(item.text)
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

        bot.enrich_item_media(item)
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
                "✅ Mirror posted\n"
                f"Source feed: @shikshanewsrajasthan\n"
                f"Title: {item.title[:200]}\n"
                f"WP: {wp_link}\n"
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