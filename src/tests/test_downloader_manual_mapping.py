from types import SimpleNamespace
from unittest.mock import Mock

from RTN import ParsedData, Torrent
from program.media.item import Movie
from program.media.item import Episode, Season, Show
from program.media.state import States
from program.media.stream import Stream
from program.services.downloaders import Downloader
from program.services.downloaders.models import (
    DebridFile,
    DownloadedTorrent,
    TorrentContainer,
    TorrentInfo,
)


def _build_show_tree() -> tuple[Show, Episode, Episode]:
    show = Show(
        {
            "title": "Example Show",
            "imdb_id": "tt9999999",
            "requested_by": "test",
        }
    )
    season = Season({"number": 1})
    episode_1 = Episode({"number": 1})
    episode_2 = Episode({"number": 2})

    # Give deterministic IDs so duplicate-suppression logic behaves like persisted rows.
    show.id = 1
    season.id = 2
    episode_1.id = 101
    episode_2.id = 102

    season.add_episode(episode_1)
    season.add_episode(episode_2)
    show.add_season(season)

    return show, episode_1, episode_2


def _build_download_result(files: list[DebridFile]) -> DownloadedTorrent:
    container = TorrentContainer(
        infohash="abc123",
        files=files,
        torrent_id=55,
        torrent_info=TorrentInfo(id=55, name="example-show-pack"),
    )
    return DownloadedTorrent(
        id=55,
        infohash="abc123",
        container=container,
        info=TorrentInfo(id=55, name="example-show-pack"),
    )


def _build_stream(raw_title: str, infohash: str) -> Stream:
    parsed_data = ParsedData(parsed_title=raw_title, raw_title=raw_title)
    torrent = Torrent(
        raw_title=raw_title,
        infohash=infohash,
        data=parsed_data,
        rank=1,
        lev_ratio=1.0,
    )
    return Stream(torrent)


def _build_movie() -> Movie:
    return Movie(
        {
            "title": "Example Movie",
            "imdb_id": "tt1234567",
            "requested_by": "test",
        }
    )


def test_update_item_attributes_applies_explicit_episode_mapping_for_show():
    downloader = Downloader()
    service = SimpleNamespace(key="mockdebrid")
    show, episode_1, episode_2 = _build_show_tree()

    container_file = DebridFile(
        file_id=10,
        filename="totally-random-file-name.mkv",
        filesize=1_000_000_000,
        download_url=None,
    )
    requested_file = DebridFile(
        file_id=10,
        filename="ignored-by-explicit-map.mkv",
        filesize=1_000_000_000,
        download_url="https://example.test/file/10",
    )

    result = _build_download_result([container_file])
    success = downloader.update_item_attributes(
        item=show,
        download_result=result,
        service=service,
        episode_file_map={1: {2: requested_file}},
    )

    assert success is True
    assert episode_1.filesystem_entry is None
    assert episode_2.filesystem_entry is not None
    assert episode_2.filesystem_entry.download_url == "https://example.test/file/10"
    assert episode_2.active_stream is not None
    assert episode_2.active_stream.infohash == "abc123"
    assert show.active_stream is not None
    assert show.active_stream.infohash == "abc123"


def test_update_item_attributes_does_not_fallback_to_filename_parse_when_mapping_exists():
    downloader = Downloader()
    service = SimpleNamespace(key="mockdebrid")
    show, episode_1, _ = _build_show_tree()

    # This file name is parseable as S01E01, but explicit mapping should be authoritative.
    container_file = DebridFile(
        file_id=10,
        filename="Example.Show.S01E01.1080p.WEB-DL.mkv",
        filesize=1_000_000_000,
        download_url="https://example.test/file/10",
    )
    requested_file = DebridFile(
        file_id=999,  # Missing in container
        filename="not-used.mkv",
        filesize=1_000_000_000,
        download_url="https://example.test/file/999",
    )

    result = _build_download_result([container_file])
    success = downloader.update_item_attributes(
        item=show,
        download_result=result,
        service=service,
        episode_file_map={1: {1: requested_file}},
    )

    assert success is False
    assert episode_1.filesystem_entry is None
    assert show.active_stream is None


def test_start_manual_download_movie_uses_selected_file_and_updates_attributes():
    downloader = Downloader()
    service = SimpleNamespace(key="mockdebrid")
    movie = _build_movie()
    stream = _build_stream("Example.Movie.2024.1080p.WEB-DL.mkv", "moviehash123")

    movie_file = DebridFile(
        file_id=1,
        filename="Example.Movie.2024.1080p.WEB-DL.mkv",
        filesize=1_500_000_000,
        download_url="https://example.test/file/1",
    )
    other_file = DebridFile(
        file_id=2,
        filename="Unrelated.Documentary.2024.1080p.WEB-DL.mkv",
        filesize=1_000_000_000,
        download_url="https://example.test/file/2",
    )
    container = TorrentContainer(
        infohash=stream.infohash,
        files=[movie_file, other_file],
        torrent_id=88,
        torrent_info=TorrentInfo(id=88, name="example-movie-pack"),
    )

    downloader.validate_stream_on_service = Mock(return_value=container)
    downloader.download_cached_stream_on_service = Mock(
        side_effect=lambda used_stream, used_container, _: DownloadedTorrent(
            id=used_container.torrent_id or 88,
            infohash=used_stream.infohash,
            container=used_container,
            info=TorrentInfo(
                id=used_container.torrent_id or 88,
                name="example-movie-pack",
            ),
        )
    )

    success = downloader.start_manual_download(
        item=movie,
        stream=stream,
        service=service,
        file_ids=[1],
    )

    assert success is True
    assert downloader.download_cached_stream_on_service.call_count == 1
    used_container = downloader.download_cached_stream_on_service.call_args[0][1]
    assert [f.file_id for f in used_container.files] == [1]
    assert movie.filesystem_entry is not None
    assert movie.filesystem_entry.download_url == "https://example.test/file/1"
    assert movie.active_stream is not None
    assert movie.active_stream.infohash == "moviehash123"
    assert movie.last_state == States.Downloaded


def test_start_manual_download_movie_fails_when_selected_file_is_not_movie_match():
    downloader = Downloader()
    service = SimpleNamespace(key="mockdebrid")
    movie = _build_movie()
    stream = _build_stream("Example.Movie.2024.1080p.WEB-DL.mkv", "moviehash456")

    episode_like_file = DebridFile(
        file_id=7,
        filename="Example.Show.S01E01.1080p.WEB-DL.mkv",
        filesize=900_000_000,
        download_url="https://example.test/file/7",
    )
    container = TorrentContainer(
        infohash=stream.infohash,
        files=[episode_like_file],
        torrent_id=99,
        torrent_info=TorrentInfo(id=99, name="example-show-pack"),
    )

    downloader.validate_stream_on_service = Mock(return_value=container)
    downloader.download_cached_stream_on_service = Mock(
        return_value=DownloadedTorrent(
            id=99,
            infohash=stream.infohash,
            container=container,
            info=TorrentInfo(id=99, name="example-show-pack"),
        )
    )

    success = downloader.start_manual_download(
        item=movie,
        stream=stream,
        service=service,
        file_ids=[7],
    )

    assert success is False
    assert movie.filesystem_entry is None
    assert movie.active_stream is None
    assert movie.last_state != States.Downloaded
