"""Rich messages (Bot API 10.1+ sendRichMessage). PTB 21 has no native method.

Reports and lists go through send_html; short prompts stay regular messages.
API error → caller sends a plain fallback.
"""
from __future__ import annotations

import html
import logging
import re
import warnings

logger = logging.getLogger(__name__)
warnings.filterwarnings("ignore", message=r"Please use 'Bot\.editMessageText'")

LIMIT = 30000


def esc(s) -> str:
    return html.escape(str(s), quote=False)


def table(head: list[str], rows: list[list[str]], align: list[str] | None = None) -> str:
    """head is plain text; row cells are already HTML (esc / <b>)."""
    al = align or ["l"] + ["r"] * (len(head) - 1)

    def cell_align(i: int) -> str:
        return ' align="right"' if al[i] == "r" else ""

    out = [
        "<table bordered striped compact><tr>"
        + "".join(f"<th{cell_align(i)}>{esc(h)}</th>" for i, h in enumerate(head))
        + "</tr>"
    ]
    out += [
        "<tr>" + "".join(f"<td{cell_align(i)}>{c}</td>" for i, c in enumerate(r)) + "</tr>"
        for r in rows
    ]
    return "".join(out) + "</table>"


def html_to_plain(body: str) -> str:
    text = str(body)
    text = re.sub(r"<h[34][^>]*>", "", text)
    text = re.sub(r"</h[34]>", "\n", text)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</p>", "\n", text)
    text = re.sub(r"</tr>", "\n", text)
    text = re.sub(r"</t[dh]>", "  ", text)
    text = re.sub(r"</summary>", "\n", text)
    text = re.sub(r"<tg-button[^>]*>", "", text)
    text = re.sub(r"</tg-button>", "", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _markup_dict(reply_markup):
    if reply_markup is None:
        return None
    to_dict = getattr(reply_markup, "to_dict", None)
    return to_dict() if callable(to_dict) else reply_markup


async def send_html(bot, chat_id, body: str, reply_markup=None, disable_notification=None):
    if len(body) > LIMIT:
        logger.warning("Rich HTML too long (%s), skip rich", len(body))
        return False
    kw = {"chat_id": chat_id, "rich_message": {"html": body}}
    markup = _markup_dict(reply_markup)
    if markup is not None:
        kw["reply_markup"] = markup
    if disable_notification:
        kw["disable_notification"] = True
    try:
        return await bot.do_api_request("sendRichMessage", api_kwargs=kw) or True
    except Exception as exc:
        logger.warning("Rich-сообщение %s не ушло (%s) — шлю обычным", chat_id, exc)
        return False


async def edit_html(bot, chat_id, message_id, body: str, reply_markup=None) -> bool:
    kw = {"chat_id": chat_id, "message_id": message_id, "rich_message": {"html": body}}
    markup = _markup_dict(reply_markup)
    if markup is not None:
        kw["reply_markup"] = markup
    try:
        await bot.do_api_request("editMessageText", api_kwargs=kw)
        return True
    except Exception as exc:
        if "not modified" in str(exc).lower():
            return True
        logger.warning("Rich-правка %s/%s не прошла (%s)", chat_id, message_id, exc)
        return False


async def reply(message, body: str, reply_markup=None):
    """Send rich HTML, fall back to a stripped regular message."""
    bot = message.get_bot()
    result = await send_html(bot, message.chat_id, body, reply_markup=reply_markup)
    if result:
        return result
    return await bot.send_message(
        chat_id=message.chat_id,
        text=html_to_plain(body),
        reply_markup=reply_markup,
    )
