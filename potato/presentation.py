from html import escape

from telethon.errors import DocumentInvalidError, PremiumAccountRequiredError

from potato.i18n import ACTIVE_LOCALIZER


CUSTOM_EMOJI = '<tg-emoji emoji-id="5368324170671202286">👍</tg-emoji>'


async def send_html(event, message: str, *, edit: bool | None = None):
    localizer = ACTIVE_LOCALIZER.get()
    if localizer is not None:
        message = localizer.render_markup(message)
    send = event.edit if (event.out if edit is None else edit) else event.reply
    try:
        return await send(message, parse_mode="html")
    except (DocumentInvalidError, PremiumAccountRequiredError):
        if CUSTOM_EMOJI not in message:
            raise
        return await send(message.replace(CUSTOM_EMOJI, "👍"), parse_mode="html")


def safe(value: object) -> str:
    return escape(str(value), quote=True)
