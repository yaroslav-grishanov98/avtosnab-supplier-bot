from dataclasses import dataclass
from typing import Optional


class UserState:
    IDLE = "idle"
    START = "start"
    AWAITING_INN = "awaiting_inn"
    COMPANY_CONFIRMATION = "company_confirmation"
    MAIN_MENU = "main_menu"
    WAITING_INVITE_CODE = "waiting_invite_code"
    PENDING_PRICE_UPLOAD = "pending_price_upload"
    PRICE_PROCESSING = "price_processing"
    AWAITING_UPLOAD_CONFIRMATION = "awaiting_upload_confirmation"
    PRICE_PROCESSED = "price_processed"
    REGISTRATION_CANCELLED = "registration_cancelled"
    SELECTING_COMPANY = "selecting_company"  # Новое: выбор компании из списка

    @classmethod
    def all_states(cls) -> list:
        return [v for k, v in vars(cls).items() if not k.startswith("_") and isinstance(v, str)]


@dataclass
class UserContext:
    chat_id: str
    telegram_user_id: str
    username: str = ""
    first_name: str = ""
    last_name: str = ""
    message_id: str = ""
    text: str = ""
    callback_data: str = ""
    callback_query_id: str = ""
    is_callback_query: bool = False
    route: str = "unknown"
    message_type: str = "unknown"
    file_id: Optional[str] = None
    file_name: Optional[str] = None
    mime_type: Optional[str] = None
    has_file: bool = False
    supplier_id: Optional[str] = None
    supplier_name: Optional[str] = None
    binding: Optional[dict] = None
    state: str = "start"
    bot_user_state: Optional[dict] = None
    upload_id: Optional[str] = None
    inn: Optional[str] = None
    company_name: Optional[str] = None
    # Новые поля для работы с несколькими компаниями
    companies: Optional[list] = None  # Список всех компаний поставщика
    selected_company_id: Optional[str] = None  # ID выбранной компании

    @property
    def is_bound(self) -> bool:
        return bool(self.supplier_id)
