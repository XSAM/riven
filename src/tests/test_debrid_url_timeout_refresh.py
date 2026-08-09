from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import trio

from program.services.streaming.media_stream import MediaStream
from program.utils.debrid_cdn_url import DebridCDNUrl

OLD_URL = "https://example.com/expired"
NEW_URL = "https://example.com/refreshed"


class _SyncStreamContext:
    def __init__(self, url: str, requested_urls: list[str]) -> None:
        self.url = url
        self.requested_urls = requested_urls

    def __enter__(self):
        self.requested_urls.append(self.url)

        if self.url == OLD_URL:
            raise httpx.ReadTimeout(
                "timed out",
                request=httpx.Request("GET", self.url),
            )

        return SimpleNamespace(raise_for_status=lambda: None)

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        return None


class _SyncClient:
    def __init__(self, requested_urls: list[str]) -> None:
        self.requested_urls = requested_urls

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        return None

    def stream(self, method: str, url: str) -> _SyncStreamContext:
        assert method == "GET"

        return _SyncStreamContext(url, self.requested_urls)


class _AsyncStreamContext:
    def __init__(self, url: str, requested_urls: list[str]) -> None:
        self.url = url
        self.requested_urls = requested_urls

    async def __aenter__(self):
        self.requested_urls.append(self.url)

        if self.url == OLD_URL:
            raise httpx.ReadTimeout(
                "timed out",
                request=httpx.Request("GET", self.url),
            )

        return SimpleNamespace(
            headers={},
            status_code=206,
            raise_for_status=lambda: None,
        )

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        return None


class _AsyncClient:
    def __init__(self, requested_urls: list[str]) -> None:
        self.requested_urls = requested_urls

    def stream(self, *, method: str, url: str, **_kwargs) -> _AsyncStreamContext:
        assert method == "GET"

        return _AsyncStreamContext(url, self.requested_urls)


class _BodyResponse:
    def __init__(self, url: str) -> None:
        self.url = url
        self.headers = {}
        self.status_code = 206

    def raise_for_status(self) -> None:
        return None

    async def aread(self) -> bytes:
        if self.url == OLD_URL:
            raise httpx.ReadTimeout(
                "timed out",
                request=httpx.Request("GET", self.url),
            )

        return b"x"


class _BodyStreamContext:
    def __init__(self, url: str, requested_urls: list[str]) -> None:
        self.url = url
        self.requested_urls = requested_urls

    async def __aenter__(self) -> _BodyResponse:
        self.requested_urls.append(self.url)

        return _BodyResponse(self.url)

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        return None


class _BodyAsyncClient:
    def __init__(self, requested_urls: list[str]) -> None:
        self.requested_urls = requested_urls

    def stream(self, *, method: str, url: str, **_kwargs) -> _BodyStreamContext:
        assert method == "GET"

        return _BodyStreamContext(url, self.requested_urls)


def test_cdn_validation_refreshes_url_after_timeout(monkeypatch) -> None:
    requested_urls: list[str] = []
    entry = SimpleNamespace(
        original_filename="video.mkv",
        unrestricted_url=OLD_URL,
        provider="realdebrid",
    )
    cdn_url = DebridCDNUrl(entry)
    refresh = Mock(return_value=NEW_URL)

    monkeypatch.setattr(
        "program.utils.debrid_cdn_url.httpx.Client",
        lambda **_kwargs: _SyncClient(requested_urls),
    )
    monkeypatch.setattr(cdn_url, "_refresh", refresh)

    assert cdn_url.validate() == NEW_URL
    refresh.assert_called_once_with()
    assert requested_urls == [OLD_URL, NEW_URL]


def test_streaming_refreshes_url_after_timeout() -> None:
    async def scenario() -> None:
        requested_urls: list[str] = []
        stream = object.__new__(MediaStream)
        stream.async_client = _AsyncClient(requested_urls)
        stream.enable_tracing = False
        stream.file_metadata = SimpleNamespace(file_size=1)
        stream.provider = "realdebrid"
        stream.session_statistics = SimpleNamespace(total_session_connections=0)
        stream.target_url = SimpleNamespace(value=OLD_URL)
        stream.build_log_message = lambda message: message

        async def refresh_url() -> bool:
            stream.target_url.value = NEW_URL

            return True

        stream._refresh_download_url = AsyncMock(side_effect=refresh_url)
        stream._retry_with_backoff = AsyncMock(return_value=True)

        async with stream.establish_connection(start=0, end=0):
            pass

        stream._refresh_download_url.assert_awaited_once_with()
        assert requested_urls == [OLD_URL, NEW_URL]

    trio.run(scenario)


def test_discrete_read_reopens_refreshed_url_after_body_timeout() -> None:
    async def scenario() -> None:
        requested_urls: list[str] = []
        stream = object.__new__(MediaStream)
        stream.async_client = _BodyAsyncClient(requested_urls)
        stream.enable_tracing = False
        stream.file_metadata = SimpleNamespace(file_size=1)
        stream.provider = "realdebrid"
        stream.session_statistics = SimpleNamespace(
            bytes_transferred=0,
            total_session_connections=0,
        )
        stream.target_url = SimpleNamespace(value=OLD_URL)
        stream.build_log_message = lambda message: message
        stream._cache_chunk = AsyncMock()

        async def refresh_url() -> bool:
            stream.target_url.value = NEW_URL

            return True

        stream._refresh_download_url = AsyncMock(side_effect=refresh_url)
        stream._retry_with_backoff = AsyncMock(return_value=True)

        assert (
            await stream._fetch_discrete_byte_range(
                start=0,
                size=1,
                should_cache=False,
            )
            == b"x"
        )
        stream._refresh_download_url.assert_awaited_once_with()
        assert requested_urls == [OLD_URL, NEW_URL]

    trio.run(scenario)
