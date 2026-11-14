"""telegram_api.py

Minimal Telegram Bot API client built on the standard library.

Only the handful of calls the cron runner needs are implemented: fetch updates,
download a document, send a text message and send a document. Using urllib
keeps the deployment dependency list unchanged, and a cron tick has no need for
long polling, webhooks or an event loop.

The bot token is read from TELEGRAM_BOT_TOKEN unless passed explicitly. It is
never written to logs or included in error messages.
"""

from __future__ import annotations

import json
import mimetypes
import os
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

API_ROOT = "https://api.telegram.org"
MAX_MESSAGE = 4096
MAX_CAPTION = 1024


class TelegramError(RuntimeError):
    """A Bot API call failed.

    `retry_after` is set when Telegram rate-limited the call (HTTP 429), in
    seconds. Callers that send in bulk should sleep that long and retry.
    """

    def __init__(self, message: str, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def _failure(exc: HTTPError) -> tuple[str, int | None]:
    """Turn an HTTPError into (description, retry_after)."""
    raw = exc.read()
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return (raw.decode("utf-8", "replace")[:300] or f"HTTP {exc.code}"), None
    if not isinstance(payload, dict):
        return str(payload)[:300], None
    description = str(payload.get("description") or payload)
    parameters = payload.get("parameters") or {}
    try:
        retry_after = int(parameters.get("retry_after")) if parameters else None
    except (TypeError, ValueError):
        retry_after = None
    return description, retry_after


def _chunks(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return chunks


def _safe_filename(name: str) -> str:
    cleaned = "".join(c if 32 <= ord(c) < 127 and c not in '"\\' else "_" for c in name)
    return cleaned or "document"


def _multipart(fields: dict[str, str], file_field: str, filename: str,
               content: bytes, content_type: str) -> tuple[str, bytes]:
    boundary = "----automatefreelance" + uuid.uuid4().hex
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n".encode("utf-8")
        )
    safe = _safe_filename(filename)
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; '
        f'filename="{safe}"\r\nContent-Type: {content_type}\r\n\r\n'.encode("utf-8")
    )
    parts.append(content)
    parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode("ascii"))
    return boundary, b"".join(parts)


class TelegramClient:
    def __init__(self, token: str | None = None, timeout: float = 30.0) -> None:
        self.token = token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
        if not self.token:
            raise TelegramError("TELEGRAM_BOT_TOKEN is not set")
        self.timeout = timeout

    # -- transport ------------------------------------------------------

    def _call(self, method: str, payload: dict | None = None,
              timeout: float | None = None) -> object:
        body = json.dumps(payload or {}).encode("utf-8")
        request = Request(
            f"{API_ROOT}/bot{self.token}/{method}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=timeout or self.timeout) as response:
                raw = response.read()
        except HTTPError as exc:
            description, retry_after = _failure(exc)
            raise TelegramError(f"{method}: {description}", retry_after) from None
        except URLError as exc:
            raise TelegramError(f"{method}: {exc.reason}") from None
        try:
            data = json.loads(raw)
        except ValueError:
            raise TelegramError(f"{method}: invalid JSON response") from None
        if not data.get("ok"):
            raise TelegramError(f"{method}: {data.get('description', 'request failed')}")
        return data.get("result")

    def _download(self, file_path: str, dest: str) -> None:
        url = f"{API_ROOT}/file/bot{self.token}/{quote(file_path, safe='/')}"
        try:
            with urlopen(Request(url), timeout=max(self.timeout, 120.0)) as response:
                data = response.read()
        except HTTPError as exc:
            description, retry_after = _failure(exc)
            raise TelegramError(f"download: {description}", retry_after) from None
        except URLError as exc:
            raise TelegramError(f"download: {exc.reason}") from None
        directory = os.path.dirname(os.path.abspath(dest))
        os.makedirs(directory, exist_ok=True)
        with open(dest, "wb") as fh:
            fh.write(data)

    # -- Bot API methods ------------------------------------------------

    def get_me(self) -> dict:
        return self._call("getMe")  # type: ignore[return-value]

    def get_updates(self, offset: int | None = None, limit: int = 100,
                    timeout: int = 0) -> list[dict]:
        payload: dict[str, object] = {
            "limit": limit,
            "timeout": timeout,
            "allowed_updates": ["message"],
        }
        if offset is not None:
            payload["offset"] = offset
        result = self._call("getUpdates", payload, timeout=timeout + 30)
        return result or []  # type: ignore[return-value]

    def get_file_path(self, file_id: str) -> str:
        result = self._call("getFile", {"file_id": file_id})
        path = (result or {}).get("file_path")  # type: ignore[union-attr]
        if not path:
            raise TelegramError("getFile: response had no file_path")
        return str(path)

    def download_file(self, file_id: str, dest: str) -> None:
        self._download(self.get_file_path(file_id), dest)

    def send_message(self, chat_id: int | str, text: str) -> None:
        for chunk in _chunks(text, MAX_MESSAGE):
            self._call("sendMessage", {
                "chat_id": chat_id,
                "text": chunk,
                "disable_web_page_preview": True,
            })

    def send_document(self, chat_id: int | str, path: str,
                      caption: str | None = None, filename: str | None = None) -> None:
        with open(path, "rb") as fh:
            content = fh.read()
        name = filename or os.path.basename(path)
        content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        fields = {"chat_id": str(chat_id)}
        if caption:
            fields["caption"] = caption[:MAX_CAPTION]
        boundary, body = _multipart(fields, "document", name, content, content_type)
        request = Request(
            f"{API_ROOT}/bot{self.token}/sendDocument",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=max(self.timeout, 120.0)) as response:
                raw = response.read()
        except HTTPError as exc:
            description, retry_after = _failure(exc)
            raise TelegramError(f"sendDocument: {description}", retry_after) from None
        except URLError as exc:
            raise TelegramError(f"sendDocument: {exc.reason}") from None
        try:
            data = json.loads(raw)
        except ValueError:
            raise TelegramError("sendDocument: invalid JSON response") from None
        if not data.get("ok"):
            raise TelegramError(f"sendDocument: {data.get('description', 'request failed')}")
