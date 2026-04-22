from dataclasses import dataclass, field


@dataclass
class ParsedMessage:
    message_id: str
    chat_id: str
    sender_name: str
    chat_name: str
    text: str
    image_urls: list[str] = field(default_factory=list)
    video_urls: list[str] = field(default_factory=list)
    file_urls: list[str] = field(default_factory=list)
    # URL -> исходное имя файла (если удалось определить в MAX).
    file_names_by_url: dict[str, str] = field(default_factory=dict)
    unknown_attachments: list[str] = field(default_factory=list)
    # Ответ в MAX: Message.link указывает на исходное сообщение (тред).
    reply_to_max_message_id: str | None = None
    reply_preview_text: str | None = None
