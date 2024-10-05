from typing import Sequence
from datetime import datetime

from pydantic import BaseModel


class Service(BaseModel):
    name: str | None = None
    host: str
    folders: Sequence[str] = ("INBOX", )
    encoding: str | None = "UTF-8"  # "US-ASCII"


class EmailMessage(BaseModel):
    date:     datetime
    id:       str
    text:     str
    subject:  str | None = None
    sender:   str | None = None
    receiver: str | None = None
    folder:   str
