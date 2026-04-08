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
