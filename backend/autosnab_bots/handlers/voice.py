"""
Обработчик голосовых сообщений.
Распознаёт речь и сразу выполняет действие — без промежуточных сообщений.
"""

from api.ai import speech_to_text
from models import UserContext

from handlers.common.messenger import telegram_messenger

msg = telegram_messenger


async def handle_voice(context: UserContext) -> None:
    """Принять голосовое сообщение, распознать текст, сразу обработать."""
    voice_file = await msg.get_file(context.file_id)
    audio_bytes = await voice_file.download_as_bytearray()

    text = await speech_to_text(audio_bytes)

    if not text:
        await msg.send_message(context.chat_id, "Не удалось распознать речь. Попробуйте ещё раз или напишите текстом.")
        return

    context.text = text
    context.message_type = "text"
    from handlers.router import dispatch

    await dispatch(context)
