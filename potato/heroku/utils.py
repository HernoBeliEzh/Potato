import html
import shlex


def get_args_raw(message):
    parts = (getattr(message, "raw_text", "") or "").split(maxsplit=1)
    return parts[1] if len(parts) > 1 else ""


def get_args(message):
    return shlex.split(get_args_raw(message))


def escape_html(value):
    return html.escape(str(value), quote=False)


def get_chat_id(message):
    return message.chat_id


def get_topic(message):
    return getattr(getattr(message, "reply_to", None), "reply_to_top_id", None)


def chunks(values, size):
    return [values[index:index + size] for index in range(0, len(values), size)]


async def answer(message, text, **kwargs):
    if kwargs.get("reply_markup"):
        raise RuntimeError("Inline-интерфейсы Heroku пока не поддерживаются")
    kwargs.pop("reply_markup", None)
    kwargs.setdefault("parse_mode", "html")
    return await (message.edit(text, **kwargs) if getattr(message, "out", False) else message.reply(text, **kwargs))


async def answer_file(message, file, **kwargs):
    return await message.client.send_file(message.chat_id, file, **kwargs)
