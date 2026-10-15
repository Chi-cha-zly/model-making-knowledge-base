"""Compatibility exports; chat implementation lives in its domain layers."""

from .api.chat import install_chat as install_chat
from .services.chat import ChatService as ChatService
from .chat_schemas import (
    AIProvider as AIProvider,
    TitleInput as TitleInput,
    RequestInput as RequestInput,
    TurnInput as TurnInput,
)
from .prompts import SYSTEM as SYSTEM
from .repositories.chat import (
    ACTIVE as ACTIVE,
    evidence as evidence,
    record as record,
    get as get,
    export_chat as export_chat,
    restore_chat as restore_chat,
)
from .chat_codec import validate_chat_backup as validate_chat_backup
