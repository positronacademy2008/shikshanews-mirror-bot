"""Verify BOT_TOKEN, destination channel, and admin chat without printing secrets."""
from __future__ import annotations

import os
import sys

import requests

from env_loader import load_dotenv

load_dotenv()

TOKEN = os.environ.get("BOT_TOKEN", "").strip()
DEST = os.environ.get("DEST_CHANNEL", "@testsourcechannelA").strip()
ADMIN = os.environ.get("ADMIN_CHAT_ID", "").strip()


def mask(token: str) -> str:
    if len(token) < 12:
        return "(missing or too short)"
    return f"{token[:6]}...{token[-4:]}"


def tg(method: str, **params) -> dict:
    if not TOKEN:
        return {"ok": False, "description": "BOT_TOKEN missing"}
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    response = requests.post(url, json=params, timeout=25)
    try:
        return response.json()
    except Exception:
        return {"ok": False, "description": response.text[:300]}


def main() -> int:
    ok = True
    print(f"BOT_TOKEN: {mask(TOKEN)}")
    print(f"DEST_CHANNEL: {DEST}")
    print(f"ADMIN_CHAT_ID: {ADMIN}")

    me = tg("getMe")
    if me.get("ok"):
        print(f"getMe OK: @{me['result'].get('username', '?')} (id={me['result'].get('id')})")
    else:
        print(f"getMe FAIL: {me.get('description')}")
        ok = False

    for label, chat_id in (("dest", DEST), ("admin", ADMIN)):
        if not chat_id:
            print(f"getChat {label}: skipped (empty)")
            continue
        chat = tg("getChat", chat_id=chat_id)
        if chat.get("ok"):
            result = chat["result"]
            print(
                f"getChat {label} OK: type={result.get('type')} "
                f"title={result.get('title', result.get('first_name', chat_id))} "
                f"id={result.get('id')}"
            )
        else:
            print(f"getChat {label} FAIL: {chat.get('description')}")
            ok = False

    if DEST and me.get("ok"):
        member = tg("getChatMember", chat_id=DEST, user_id=me["result"]["id"])
        if member.get("ok"):
            status = member["result"].get("status")
            can_post = member["result"].get("can_post_messages")
            print(f"getChatMember dest OK: status={status} can_post_messages={can_post}")
            if status not in {"administrator", "creator"}:
                print("WARNING: Bot is not channel administrator")
                ok = False
            elif can_post is False:
                print("WARNING: Bot admin but can_post_messages=false")
                ok = False
        else:
            print(f"getChatMember dest FAIL: {member.get('description')}")
            ok = False

    if ADMIN and me.get("ok"):
        member = tg("getChatMember", chat_id=ADMIN, user_id=me["result"]["id"])
        if member.get("ok"):
            print(f"getChatMember admin OK: status={member['result'].get('status')}")
        else:
            print(f"getChatMember admin FAIL: {member.get('description')}")
            print("Hint: Open @rssfeedpositronbot and press Start if not done.")

    if ok and DEST:
        test = tg(
            "sendMessage",
            chat_id=DEST,
            text="🔧 Positron mirror bot — Telegram permission test (ignore/delete this message).",
            disable_web_page_preview=True,
        )
        if test.get("ok"):
            print(f"sendMessage dest OK: message_id={test['result'].get('message_id')}")
        else:
            print(f"sendMessage dest FAIL: {test.get('description')}")
            ok = False

    if ok and ADMIN:
        test = tg(
            "sendMessage",
            chat_id=ADMIN,
            text="🔧 Positron mirror bot — admin DM test OK.",
            disable_web_page_preview=True,
        )
        if test.get("ok"):
            print(f"sendMessage admin OK: message_id={test['result'].get('message_id')}")
        else:
            print(f"sendMessage admin FAIL: {test.get('description')}")
            ok = False

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())