"""Pydantic models mirroring the Yandex Messenger Bot API data structures."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class YmSender(BaseModel):
    login: str | None = None
    id: str | None = None
    display_name: str | None = None
    robot: bool = False


class YmChat(BaseModel):
    id: str
    type: str  # "private", "group", "channel"


class YmFile(BaseModel):
    id: str = ""
    name: str = ""
    size: int = 0


class YmImage(BaseModel):
    file_id: str = ""
    width: int = 0
    height: int = 0
    size: int | None = None
    name: str | None = None


class YmServerAction(BaseModel):
    name: str = ""
    payload: dict[str, Any] = {}


class YmBotRequest(BaseModel):
    server_action: YmServerAction | None = None
    element_id: str = ""
    errors: list[dict[str, Any]] = []


class YmUpdate(BaseModel):
    update_id: int = 0
    message_id: int = 0
    timestamp: int = 0
    text: str | None = None
    chat: YmChat | None = None
    from_: YmSender | None = None
    file: YmFile | None = None
    images: list[Any] | None = None  # Can be Image[] or Image[][] per API quirk
    callback_data: dict[str, Any] | None = None
    bot_request: YmBotRequest | None = None

    model_config = {"populate_by_name": True}

    def __init__(self, **data: Any) -> None:
        # Yandex uses "from" as a key which is a Python reserved word
        if "from" in data:
            data["from_"] = data.pop("from")
        super().__init__(**data)


class YmUpdatesResponse(BaseModel):
    ok: bool = True
    updates: list[YmUpdate] = []


class YmSendTextResponse(BaseModel):
    ok: bool
    message_id: int | None = None
    code: str | None = None
    description: str | None = None


class YmSelfUpdateResponse(BaseModel):
    ok: bool
    webhook_url: str | None = None
    id: str | None = None
    display_name: str | None = None
    login: str | None = None


class YmSuggestButton(BaseModel):
    id: str | None = None
    title: str
    directives: list[dict[str, Any]] = []


class YmSuggestButtons(BaseModel):
    layout: str | None = None
    persist: bool | None = None
    buttons: list[YmSuggestButton] = []
