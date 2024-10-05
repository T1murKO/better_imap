import asyncio
from datetime import datetime, timedelta
from typing import Literal, Sequence, AsyncGenerator
from email import message_from_bytes
from email.utils import parsedate_to_datetime
import re

import pytz
from better_proxy import Proxy

from .proxy import IMAP4_SSL_PROXY
from .errors import IMAPSearchTimeout
from .errors import IMAPLoginFailed
from .models import EmailMessage
from .models import Service
from .utils import extract_email_text


class MailBox:
    def __init__(
        self,
        service: Service,
        address: str,
        password: str,
        *,
        proxy: Proxy = None,
        timeout: float = 10,
    ):
        if service.host == "imap.rambler.ru" and "%" in password:
            raise ValueError(
                f"IMAP password contains '%' character. Change your password."
                f" It's a specific rambler.ru error"
            )

        self._address = address
        self._password = password
        self._service = service
        self._connected = False
        self._imap = IMAP4_SSL_PROXY(
            host=service.host,
            proxy=proxy,
            timeout=timeout,
        )
        self._folder = None

    async def __aenter__(self):
        await self.login()
        return self

    async def __aexit__(self, *args):
        await self.logout()

    async def logout(self):
        await self._imap.logout()

    async def login(self):
        if self._connected:
            return

        await self._imap.wait_hello_from_server()
        await self._imap.login(self._address, self._password)
        if self._imap.get_state() == "NONAUTH":
            raise IMAPLoginFailed()
        self._connected = True

    async def select_folder(self, folder: str):
        self._folder = folder
        return await self._imap.select(mailbox=folder)

    async def fetch_message_by_id(self, id: str) -> EmailMessage:
        typ, msg_data = await self._imap.fetch(id, "(RFC822)")
        if typ == "OK":
            email_bytes = bytes(msg_data[1])
            email_message = message_from_bytes(email_bytes)
            email_sender = email_message.get("from")
            email_receiver = email_message.get("to")
            subject = email_message.get("subject")
            email_date = parsedate_to_datetime(email_message.get("date"))

            if email_date.tzinfo is None:
                email_date = pytz.utc.localize(email_date)
            elif email_date.tzinfo != pytz.utc:
                email_date = email_date.astimezone(pytz.utc)

            message_text = extract_email_text(email_message)
            return EmailMessage(
                id=id,
                text=message_text,
                date=email_date,
                sender=email_sender,
                receiver=email_receiver,
                subject=subject,
                folder=self._folder,
            )

    async def fetch_messages(
        self,
        folder: str,
        *,
        search_criteria: Literal["ALL", "UNSEEN"] = "ALL",
        since: datetime = None,
        allowed_senders: Sequence[str] = None,
        allowed_receivers: Sequence[str] = None,
        sender_regex: str | re.Pattern[str] = None,
    ) -> list[EmailMessage]:
        await self.select_folder(folder)

        if since:
            date_filter = since.strftime("%d-%b-%Y")
            search_criteria += f" SINCE {date_filter}"

        if allowed_senders:
            senders_criteria = " ".join(
                [f'FROM "{sender}"' for sender in allowed_senders]
            )
            search_criteria += f" {senders_criteria}"

        if allowed_receivers:
            receivers_criteria = " ".join(
                [f'TO "{receiver}"' for receiver in allowed_receivers]
            )
            search_criteria += f" {receivers_criteria}"

        status, data = await self._imap.search(
            search_criteria, charset=self._service.encoding
        )

        if status != "OK":
            return []

        if not data[0]:
            return []

        email_ids = data[0].split()
        email_ids = email_ids[::-1]
        messages = []
        for e_id_str in email_ids:
            message = await self.fetch_message_by_id(
                e_id_str.decode(self._service.encoding)
            )

            if since and message.date < since:
                continue

            if sender_regex and not re.search(
                sender_regex, message.sender, re.IGNORECASE
            ):
                continue

            messages.append(message)

        return messages

    async def search_once(
        self,
        regex: str | re.Pattern[str],
        folders: Sequence[str] = None,
        *,
        since: datetime,
        search_criteria: Literal["ALL", "UNSEEN"] = "ALL",
        allowed_senders: Sequence[str] = None,
        allowed_receivers: Sequence[str] = None,
        sender_regex: str | re.Pattern[str] = None,
    ) -> list[tuple[EmailMessage, str | None]]:
        folders = folders or self._service.folders

        matches = []
        for folder in folders:
            messages = await self.fetch_messages(
                folder,
                since=since,
                search_criteria=search_criteria,
                allowed_senders=allowed_senders,
                allowed_receivers=allowed_receivers,
                sender_regex=sender_regex,
            )
            for message in messages:
                if found := re.findall(regex, message.text):
                    matches.append((message, found[0]))
                else:
                    matches.append((message, None))

        return matches

    async def search(
        self,
        regex: str | re.Pattern[str],
        folders: Sequence[str] = None,
        *,
        since: datetime,
        allowed_senders: Sequence[str] = None,
        allowed_receivers: Sequence[str] = None,
        sender_email_regex: str | re.Pattern[str] = None,
        sleep_time: int = 5,
        max_search_time: int = 60,
    ) -> AsyncGenerator[list[tuple[EmailMessage, str | None]], None]:
        """
        :return: message, code
        """
        end_time = asyncio.get_event_loop().time() + max_search_time

        seen = set()

        while asyncio.get_event_loop().time() < end_time:
            matches = await self.search_once(
                regex,
                folders,
                allowed_senders=allowed_senders,
                sender_regex=sender_email_regex,
                allowed_receivers=allowed_receivers,
                since=since,
            )
            unseen = []
            for message, code in matches:
                if message.id not in seen:
                    unseen.append((message, code))
                seen.add(message.id)

            yield unseen

            await asyncio.sleep(sleep_time)

        raise IMAPSearchTimeout(f"Max search time reached ({max_search_time} sec)")

    def search_since_now(
            self,
            regex: str | re.Pattern[str],
            folders: Sequence[str] = None,
            *,
            seconds_offset: int = 0,
            allowed_senders: Sequence[str] = None,
            allowed_receivers: Sequence[str] = None,
            sender_email_regex: str | re.Pattern[str] = None,
            interval: int = 5,
            timeout: int = 60,
    ):
        since = datetime.now(pytz.utc) - timedelta(seconds=seconds_offset)
        return self.search(
            regex=regex,
            folders=folders,
            since=since,
            allowed_senders=allowed_senders,
            allowed_receivers=allowed_receivers,
            sender_email_regex=sender_email_regex,
            sleep_time=interval,
            max_search_time=timeout,
        )

    async def search_once_since_now(
        self,
        regex: str | re.Pattern[str],
        folders: Sequence[str] = None,
        *,
        hours_offset: int = 0,
        minutes_offset: int = 0,
        seconds_offset: int = 0,
        search_criteria: Literal["ALL", "UNSEEN"] = "ALL",
        allowed_senders: Sequence[str] = None,
        allowed_receivers: Sequence[str] = None,
        sender_regex: str | re.Pattern[str] = None,
    ) -> list[tuple[EmailMessage, str | None]]:
        since = datetime.now(pytz.utc) - timedelta(
            hours=hours_offset, minutes=minutes_offset, seconds=seconds_offset)
        return await self.search_once(
            regex,
            folders,
            since=since,
            search_criteria=search_criteria,
            allowed_senders=allowed_senders,
            allowed_receivers=allowed_receivers,
            sender_regex=sender_regex,
        )
