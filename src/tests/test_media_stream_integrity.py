from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import trio
import trio_util
from ordered_set import OrderedSet

from program.services.filesystem.vfs.rivenvfs import RivenVFS
from program.services.streaming.media_stream import MediaStream
from program.services.streaming.recent_reads import Read, RecentReads
from program.services.streaming.session_statistics import SessionStatistics
from program.services.streaming.stream_connection import StreamConnection


def test_concurrent_reads_start_only_one_stream() -> None:
    async def scenario() -> None:
        stream = object.__new__(MediaStream)
        stream.config = SimpleNamespace(connect_timeout_seconds=1)
        stream.is_streaming = trio_util.AsyncBool(False)
        stream.recent_reads = RecentReads()
        stream._stream_start_lock = trio.Lock()
        stream._detect_read_type = AsyncMock(return_value="body_read")

        class Nursery:
            starts = 0

            async def start(self, _async_fn, _position) -> None:
                self.starts += 1
                await trio.sleep(0)
                stream.is_streaming.value = True

        nursery = Nursery()
        stream.nursery = nursery
        chunk_range = SimpleNamespace(position=0)

        async def read() -> None:
            async with stream.read_lifecycle(chunk_range):
                pass

        async with trio.open_nursery() as test_nursery:
            test_nursery.start_soon(read)
            test_nursery.start_soon(read)

        assert nursery.starts == 1

    trio.run(scenario)


def test_backward_read_seeks_instead_of_caching_from_wrong_position() -> None:
    async def scenario() -> None:
        stream = object.__new__(MediaStream)
        stream.config = SimpleNamespace(header_size=0)
        stream.enable_tracing = False
        stream.file_metadata = SimpleNamespace(path="/video.mkv", file_size=1_000)
        stream.is_streaming = trio_util.AsyncBool(False)
        stream.is_killed = trio_util.AsyncBool(False)
        stream._stream_error = trio_util.AsyncValue(None)
        stream.target_url = trio_util.AsyncValue("https://example.com/video.mkv")
        stream.recent_reads = RecentReads()
        stream.session_statistics = SessionStatistics()
        stream.build_log_message = lambda message: message

        async def reader() -> AsyncGenerator[bytes, None]:
            yield b"bytes-from-position-300"

        response = SimpleNamespace(
            request=SimpleNamespace(url="https://example.com/video.mkv")
        )
        connection = StreamConnection(
            response=response,
            start_position=100,
            current_read_position=300,
            reader=reader(),
        )
        seek_calls = []
        original_seek = connection.seek

        def seek(chunk_range) -> None:
            seek_calls.append(chunk_range)
            original_seek(chunk_range)
            stream.is_killed.value = True

        connection.seek = seek

        @asynccontextmanager
        async def manage_connection(*, position: int):
            assert position == 100
            yield connection

        stream.manage_connection = manage_connection

        async def cache_chunk(**_kwargs) -> None:
            stream.is_killed.value = True

        stream._cache_chunk = AsyncMock(side_effect=cache_chunk)

        class Chunk:
            index = 1
            start = 200
            end = 223

            def emit_cache_signal(self) -> None:
                pass

        chunk = Chunk()
        chunk_range = SimpleNamespace(
            position=200,
            request_range=(200, 223),
            uncached_chunks=OrderedSet([chunk]),
        )

        async with trio.open_nursery() as nursery:
            await nursery.start(stream.run, 100)
            stream.recent_reads.current_read.value = Read(
                chunk_range=chunk_range,
                read_type="body_read",
            )
            with trio.fail_after(1):
                await stream.is_streaming.wait_value(False)

        assert seek_calls == [chunk_range]
        stream._cache_chunk.assert_not_awaited()

    trio.run(scenario)


def test_noncontiguous_chunks_reconnect_before_caching_gap() -> None:
    async def scenario() -> None:
        stream = object.__new__(MediaStream)
        stream.config = SimpleNamespace(header_size=0)
        stream.enable_tracing = False
        stream.file_metadata = SimpleNamespace(path="/video.mkv", file_size=1_000)
        stream.is_streaming = trio_util.AsyncBool(False)
        stream.is_killed = trio_util.AsyncBool(False)
        stream._stream_error = trio_util.AsyncValue(None)
        stream.target_url = trio_util.AsyncValue("https://example.com/video.mkv")
        stream.recent_reads = RecentReads()
        stream.session_statistics = SessionStatistics()
        stream.build_log_message = lambda message: message

        async def reader() -> AsyncGenerator[bytes, None]:
            yield b"a" * 100
            yield b"bytes-from-the-gap"

        response = SimpleNamespace(
            request=SimpleNamespace(url="https://example.com/video.mkv")
        )
        connection = StreamConnection(
            response=response,
            start_position=100,
            current_read_position=100,
            reader=reader(),
        )

        class Chunk:
            def __init__(self, index: int, start: int, end: int) -> None:
                self.index = index
                self.start = start
                self.end = end

            @property
            def size(self) -> int:
                return self.end - self.start + 1

            def emit_cache_signal(self) -> None:
                pass

        first_chunk = Chunk(index=1, start=100, end=199)
        gap_chunk = Chunk(index=2, start=300, end=399)
        gap_range = SimpleNamespace(
            position=300,
            request_range=(300, 399),
            uncached_chunks=OrderedSet([gap_chunk]),
        )
        stream.chunker = SimpleNamespace(get_chunk_range=lambda **_kwargs: gap_range)

        seek_calls = []
        original_seek = connection.seek

        def seek(chunk_range) -> None:
            seek_calls.append(chunk_range)
            original_seek(chunk_range)
            stream.is_killed.value = True

        connection.seek = seek

        @asynccontextmanager
        async def manage_connection(*, position: int):
            assert position == 100
            yield connection

        stream.manage_connection = manage_connection

        async def cache_chunk(**kwargs) -> None:
            if kwargs["start"] == gap_chunk.start:
                stream.is_killed.value = True

        stream._cache_chunk = AsyncMock(side_effect=cache_chunk)
        requested_range = SimpleNamespace(
            position=100,
            request_range=(100, 399),
            uncached_chunks=OrderedSet([first_chunk, gap_chunk]),
        )

        async with trio.open_nursery() as nursery:
            await nursery.start(stream.run, 100)
            stream.recent_reads.current_read.value = Read(
                chunk_range=requested_range,
                read_type="body_read",
            )
            with trio.fail_after(1):
                await stream.is_streaming.wait_value(False)

        assert seek_calls == [gap_range]
        stream._cache_chunk.assert_awaited_once_with(
            start=first_chunk.start,
            data=b"a" * 100,
        )

    trio.run(scenario)


def test_concurrent_stream_lookup_creates_one_media_stream(monkeypatch) -> None:
    async def scenario() -> None:
        vfs = object.__new__(RivenVFS)
        vfs._active_streams = {}
        vfs._active_streams_lock = trio.Lock()
        vfs.stream_nursery = object()
        vfs.vfs_db = SimpleNamespace(
            get_entry_by_original_filename=lambda **_kwargs: SimpleNamespace(
                url="https://example.com/video.mkv",
                provider="realdebrid",
            )
        )
        created_streams = []

        def create_stream(**kwargs):
            stream = SimpleNamespace(**kwargs)
            created_streams.append(stream)
            return stream

        monkeypatch.setattr(
            "program.services.filesystem.vfs.rivenvfs.MediaStream",
            create_stream,
        )

        results = []

        async def lookup() -> None:
            results.append(
                await vfs._get_stream(
                    path="/video.mkv",
                    fh=1,
                    file_size=1_000,
                    original_filename="video.mkv",
                )
            )

        async with trio.open_nursery() as nursery:
            nursery.start_soon(lookup)
            nursery.start_soon(lookup)

        assert len(created_streams) == 1
        assert results == [created_streams[0], created_streams[0]]

    trio.run(scenario)
