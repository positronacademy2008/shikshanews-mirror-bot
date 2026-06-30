"""Shiksha News Rajasthan mirror bot — indianaukrihelp.com pages to positronacademy.in."""
from __future__ import annotations

import logging
import mimetypes
import os
import re
import time
from typing import Any

import requests
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

MEDIA_ONLY_WP_MARKER = "media-only"
TELEGRAM_MENTION_RE = re.compile(r"(?<!\w)@[A-Za-z][A-Za-z0-9_]{2,}")

_original_catchup_wordpress_links = bot.MirrorBot.catchup_wordpress_links
_original_build_caption = run_bot.build_caption
_mirror_send_pdf_item = bot.MirrorBot.send_pdf_item


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


def _telegram_handle_replace() -> str:
    handle = os.environ.get("TELEGRAM_HANDLE_REPLACE", "@KapilRJ06").strip() or "@KapilRJ06"
    return handle if handle.startswith("@") else f"@{handle}"


def _replace_telegram_mentions(text: str) -> str:
    """Swap feed/source @channel or @user mentions with the configured handle."""
    replacement = _telegram_handle_replace()
    return TELEGRAM_MENTION_RE.sub(replacement, text or "")


def _strip_feed_channel_refs(text: str) -> str:
    def url_replace(match: re.Match[str]) -> str:
        url = bot.clean_url(match.group(1))
        return "" if _is_telegram_channel_url(url) else url

    cleaned = bot.URL_RE.sub(url_replace, text or "")
    cleaned = _replace_telegram_mentions(cleaned)

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


def _is_preview_media_url(url: str) -> bool:
    lower = bot.clean_url(url).lower()
    return "preview" in lower or "thumb" in lower


def _collect_cdn_media_urls(item: bot.FeedItem) -> tuple[list[str], list[str]]:
    pdf_urls: list[str] = []
    image_urls: list[str] = []
    sources = [item.enclosure_url or "", item.text or "", item.html_content or ""]
    for chunk in sources:
        for url in bot.extract_urls(chunk):
            clean = bot.clean_url(url)
            if not _is_media_cdn_url(clean):
                continue
            if _is_pdf_url(clean):
                if clean not in pdf_urls:
                    pdf_urls.append(clean)
            elif bot.looks_like_real_image(clean) and clean not in image_urls:
                image_urls.append(clean)
    return pdf_urls, image_urls


def _pick_best_image_url(image_urls: list[str]) -> str:
    if not image_urls:
        return ""
    full = [url for url in image_urls if not _is_preview_media_url(url)]
    return (full or image_urls)[0]


def _set_image_enclosure(item: bot.FeedItem, url: str, content_type: str = "") -> None:
    guessed = content_type or mimetypes.guess_type(url)[0] or "image/jpeg"
    if not guessed.startswith("image/"):
        guessed = "image/jpeg"
    item.enclosure_url = url
    item.enclosure_type = bot.normalize_mime(guessed)


def _probe_remote_mime(url: str, session: requests.Session | None, feed_url: str) -> str:
    if not url or not session:
        return ""
    headers = bot.default_headers(feed_url)
    for method in ("head", "get"):
        try:
            if method == "head":
                response = session.head(url, headers=headers, timeout=20, allow_redirects=True, verify=True)
            else:
                response = session.get(url, headers=headers, timeout=25, stream=True, verify=True)
                response.raise_for_status()
            ctype = bot.normalize_mime(response.headers.get("Content-Type", ""))
            if ctype:
                return ctype
        except Exception:
            continue
    return ""


def _normalize_media_enclosure(
    item: bot.FeedItem,
    session: requests.Session | None = None,
    feed_url: str = "",
) -> None:
    """Keep Telegram/RSS CDN photos and PDFs; prefer real images over mislabeled .pdf attachments."""
    pdf_urls, image_urls = _collect_cdn_media_urls(item)
    best_image = _pick_best_image_url(image_urls)

    if pdf_urls:
        primary_pdf = pdf_urls[0]
        probed = _probe_remote_mime(primary_pdf, session, feed_url)
        if probed.startswith("image/"):
            _set_image_enclosure(item, primary_pdf, probed)
            return

        full_images = [url for url in image_urls if not _is_preview_media_url(url)]
        if full_images:
            _set_image_enclosure(item, full_images[0])
            return

        if (
            best_image
            and _is_preview_media_url(best_image)
            and probed
            and not probed.startswith("application/pdf")
        ):
            LOGGER.info(
                "Using CDN preview image instead of mislabeled attachment for: %s",
                item.title[:80],
            )
            _set_image_enclosure(item, best_image)
            return

        item.enclosure_url = primary_pdf
        item.enclosure_type = "application/pdf"
        return

    if best_image:
        _set_image_enclosure(item, best_image)
        return

    item.enclosure_url = ""
    item.enclosure_type = ""


def _enrich_media_attachment(
    item: bot.FeedItem,
    session: requests.Session | None,
    feed_url: str,
) -> None:
    _normalize_media_enclosure(item, session, feed_url)
    if item.enclosure_url and (_has_valid_photo(item) or _has_valid_pdf(item)):
        return

    bot.enrich_item_media(item)
    _normalize_media_enclosure(item, session, feed_url)
    if item.enclosure_url or not session or not item.source_url:
        return

    embed_text, embed_html, img_url, img_type = bot.fetch_telegram_embed_content(item.source_url, session)
    if embed_text and not (item.text or "").strip():
        item.text = embed_text
    if embed_html and not item.html_content:
        item.html_content = embed_html
    if img_url:
        _set_image_enclosure(item, img_url, img_type)


def _has_valid_pdf(item: bot.FeedItem) -> bool:
    url = item.enclosure_url or ""
    if not url or not _is_media_cdn_url(url):
        return False
    ctype = bot.normalize_mime(item.enclosure_type)
    if ctype.startswith("image/"):
        return False
    return ctype == "application/pdf" or _is_pdf_url(url)


def _has_valid_photo(item: bot.FeedItem) -> bool:
    url = item.enclosure_url or ""
    if not url or not _is_media_cdn_url(url):
        return False
    ctype = bot.normalize_mime(item.enclosure_type)
    if ctype.startswith("image/"):
        return True
    guessed = mimetypes.guess_type(url)[0] or ""
    if guessed.startswith("image/"):
        return True
    if _is_pdf_url(url):
        return False
    return bot.looks_like_real_image(url)


def send_pdf_item_with_image_fallback(
    self: bot.MirrorBot,
    item: bot.FeedItem,
    media_caption: str,
    fallback_text: str,
) -> None:
    try:
        response = self.session.get(
            item.enclosure_url,
            headers=bot.default_headers(self.config.feed_url),
            timeout=60,
            verify=self.config.verify_ssl,
        )
        response.raise_for_status()
        content_type = bot.normalize_mime(response.headers.get("Content-Type", ""))
        if content_type.startswith("image/"):
            item.enclosure_type = content_type
            run_bot.send_image_item(self, item, media_caption, fallback_text)
            return
    except Exception as exc:
        LOGGER.warning("PDF preflight failed for %s: %s", item.title[:80], exc)
    _mirror_send_pdf_item(self, item, media_caption, fallback_text)


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


def catchup_mirror_targets_only(self: bot.MirrorBot, feed_items: list[bot.FeedItem]) -> None:
    """Only back-fill WordPress for indianaukrihelp mirror posts — never image-only feed items."""
    if self.config.skip_wordpress or not self.wordpress.ready:
        return
    if not bot.parse_bool(os.environ.get("WP_CATCHUP"), True):
        return

    by_guid = {item.guid: item for item in feed_items}
    pending_rows = self.state.list_published_without_wp_link(limit=12)
    if not pending_rows:
        return

    eligible: list[tuple[Any, bot.FeedItem]] = []
    for row in pending_rows:
        item = by_guid.get(row["guid"])
        if not item:
            item = bot.feed_item_from_catchup_row(row, self.session)
        item.text = bot.remove_spam_urls_from_text(item.text or "")
        item.text = _strip_feed_channel_refs(item.text)
        _enrich_media_attachment(item, self.session, self.config.feed_url)
        if _has_mirror_targets(item, self.config):
            eligible.append((row, item))
        else:
            LOGGER.info("Catch-up skipped (no indianaukrihelp mirror target): %s", row["guid"])

    if not eligible:
        return

    LOGGER.info("WordPress catch-up: %s mirror-target item(s) missing post links.", len(eligible))
    for row, item in eligible:
        if self.time_budget_exceeded(reserve_seconds=60):
            LOGGER.warning("Stopping WordPress catch-up to stay inside MAX_RUN_SECONDS.")
            break
        source_page_html, page_links = "", []
        if self.config.fetch_source_for_links and item.source_url:
            source_page_html, page_links = self.fetch_source_context(item.source_url)
        wp_link = self.publish_wordpress_for_item(
            item,
            source_page_html=source_page_html,
            page_links=page_links,
        )
        if wp_link:
            self.state.set_wp_link(row["guid"], wp_link)
            LOGGER.info("Catch-up published WordPress post: %s", wp_link)
        elif not self.config.dry_run:
            LOGGER.warning("Catch-up could not create WordPress post for %s", row["guid"])


def _notify_admin(telegram: bot.TelegramClient, config: bot.Config, message: str) -> None:
    if not config.admin_chat_id:
        return
    try:
        telegram.send_text(config.admin_chat_id, message[:3900], disable_preview=False)
    except Exception as exc:
        LOGGER.warning("Admin notification failed: %s", exc)


def process_mirror_item(self: bot.MirrorBot, item: bot.FeedItem) -> None:
    LOGGER.info("Processing feed item: %s", item.title[:100])
    try:
        item.title = _replace_telegram_mentions(item.title)
        item.text = bot.remove_spam_urls_from_text(item.text)
        item.text = _strip_feed_channel_refs(item.text)
        item.text = _dedupe_title_lines(item.text, item.title)
        if bot.message_has_skip_phrase(item, self.config.skip_message_phrases):
            self.state.mark_skipped(item.guid, "Blocked phrase present")
            return
        if bot.looks_like_ad_message(item.text):
            self.state.mark_skipped(item.guid, "Advertisement/promotional message")
            return

        _enrich_media_attachment(item, self.session, self.config.feed_url)
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
        item.enclosure_url = media_enclosure_url
        item.enclosure_type = media_enclosure_type

        important_links = bot.dedupe_links(
            [
                *bot.extract_important_links(item.html_content or item.text, item.source_url),
                *bot.extract_important_links(source_page_html, item.source_url),
                *page_links,
            ],
            limit=24,
        )
        caption_source_url = self.caption_source_url(item.source_url or "", source_replacements)

        if self.config.dry_run:
            LOGGER.info("[DRY_RUN] Would send to %s: %s", self.config.dest_channels, item.title[:80])
            return

        self.dispatch_telegram(item, wp_link, important_links, caption_source_url)
        stored_wp_link = wp_link
        if not has_targets and has_media and not wp_link:
            stored_wp_link = MEDIA_ONLY_WP_MARKER
        self.state.mark_published(item.guid, stored_wp_link)

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


def _bind_select_items_newest_first() -> None:
    """RSS lists oldest posts first; mirror newest actionable items before stale skip-only rows."""
    original = bot.MirrorBot.select_items

    def wrapped(self: bot.MirrorBot, items: list[bot.FeedItem]) -> list[bot.FeedItem]:
        return original(self, list(reversed(items)))

    bot.MirrorBot.select_items = wrapped  # type: ignore[method-assign]


def build_caption_with_handle_replace(
    title: str,
    content_text: str,
    fallback_text: str,
    wp_link: str,
    source_url: str,
    important_links: list[bot.LinkInfo],
    config: bot.Config,
    limit: int,
    enclosure_url: str = "",
) -> str:
    caption = _original_build_caption(
        title,
        content_text,
        fallback_text,
        wp_link,
        source_url,
        important_links,
        config,
        limit,
        enclosure_url,
    )
    return _replace_telegram_mentions(caption)


def patch_mirror_bot() -> None:
    _bind_select_items_newest_first()
    run_bot.build_caption = build_caption_with_handle_replace
    bot.build_caption = build_caption_with_handle_replace
    bot.MirrorBot.send_pdf_item = send_pdf_item_with_image_fallback
    bot.MirrorBot.process_one = process_mirror_item
    bot.MirrorBot.catchup_wordpress_links = catchup_mirror_targets_only


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
    os.environ.setdefault("TELEGRAM_HANDLE_REPLACE", "@KapilRJ06")
    patch_mirror_bot()
    bot.main()


if __name__ == "__main__":
    main()