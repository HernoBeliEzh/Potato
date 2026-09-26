from html import escape

from telethon.errors import DocumentInvalidError, PremiumAccountRequiredError


CUSTOM_EMOJI = '<tg-emoji emoji-id="5368324170671202286">👍</tg-emoji>'


async def send_html(event, message: str, *, edit: bool = False):
    send = event.edit if edit else event.reply
    try:
        return await send(message, parse_mode="html")
    except (DocumentInvalidError, PremiumAccountRequiredError):
        if CUSTOM_EMOJI not in message:
            raise
        return await send(message.replace(CUSTOM_EMOJI, "👍"), parse_mode="html")


def safe(value: object) -> str:
    return escape(str(value), quote=True)
