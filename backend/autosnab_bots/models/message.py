class MessageType:
    TEXT = "text"
    SUPPORTED_FILE = "supported_file"
    UNSUPPORTED_FILE = "unsupported_file"
    VOICE = "voice"
    STICKER = "sticker"
    ANIMATION = "animation"
    VIDEO = "video"
    CONTACT = "contact"
    LOCATION = "location"
    UNKNOWN = "unknown"


SUPPORTED_MIME = {
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "text/csv",
    "application/csv",
    "application/vnd.ms-excel",
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/msword",
    "text/plain",
    "image/jpeg",
    "image/png",
}
