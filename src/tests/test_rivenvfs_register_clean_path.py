import threading
from unittest.mock import Mock

import pytest

pyfuse3 = pytest.importorskip("pyfuse3")

from program.services.filesystem.vfs.rivenvfs import RivenVFS
from program.services.filesystem.vfs.vfs_node import VFSDirectory, VFSFile, VFSRoot


def _make_vfs_test_instance() -> RivenVFS:
    # Avoid full RivenVFS initialization (mounting/FUSE thread). We only test
    # _register_clean_path behavior with a minimal in-memory instance.
    vfs = object.__new__(RivenVFS)
    vfs._tree_lock = threading.RLock()
    vfs._pending_invalidations = set()
    return vfs


def test_register_clean_path_refreshes_existing_file_mapping(monkeypatch):
    vfs = _make_vfs_test_instance()
    root = VFSRoot()
    existing = VFSFile(
        name="Episode.mkv",
        inode=pyfuse3.InodeT(2),
        parent=root,
        original_filename="old-source-file.mkv",
        file_size=100,
        created_at="old-created",
        updated_at="old-updated",
        entry_type="media",
    )

    monkeypatch.setattr(vfs, "_get_node_by_path", lambda _: existing)
    create_node = Mock()
    monkeypatch.setattr(vfs, "_get_or_create_node", create_node)
    get_parent_inodes = Mock(return_value=[pyfuse3.InodeT(9)])
    monkeypatch.setattr(vfs, "_get_parent_inodes", get_parent_inodes)

    result = vfs._register_clean_path(
        clean_path="/shows/example/Episode.mkv",
        original_filename="new-source-file.mkv",
        file_size=200,
        created_at="new-created",
        updated_at="new-updated",
        entry_type="media",
    )

    assert result is True
    assert existing.original_filename == "new-source-file.mkv"
    assert existing.file_size == 200
    assert existing.created_at == "new-created"
    assert existing.updated_at == "new-updated"
    assert existing.entry_type == "media"
    create_node.assert_not_called()
    get_parent_inodes.assert_not_called()
    assert vfs._pending_invalidations == set()


def test_register_clean_path_creates_node_and_tracks_invalidations(monkeypatch):
    vfs = _make_vfs_test_instance()
    root = VFSRoot()
    created_node = VFSFile(
        name="Episode.mkv",
        inode=pyfuse3.InodeT(3),
        parent=root,
        original_filename="new-source-file.mkv",
        file_size=200,
        created_at="new-created",
        updated_at="new-updated",
        entry_type="media",
    )

    get_node_by_path = Mock(return_value=None)
    create_node = Mock(return_value=created_node)
    get_parent_inodes = Mock(return_value=[pyfuse3.InodeT(11), pyfuse3.InodeT(12)])
    monkeypatch.setattr(vfs, "_get_node_by_path", get_node_by_path)
    monkeypatch.setattr(vfs, "_get_or_create_node", create_node)
    monkeypatch.setattr(vfs, "_get_parent_inodes", get_parent_inodes)

    result = vfs._register_clean_path(
        clean_path="/shows/example/Episode.mkv",
        original_filename="new-source-file.mkv",
        file_size=200,
        created_at="new-created",
        updated_at="new-updated",
        entry_type="media",
    )

    assert result is True
    create_node.assert_called_once()
    get_parent_inodes.assert_called_once_with(created_node)
    assert vfs._pending_invalidations == {pyfuse3.InodeT(11), pyfuse3.InodeT(12)}


def test_register_clean_path_returns_false_when_path_is_existing_directory(monkeypatch):
    vfs = _make_vfs_test_instance()
    existing_directory = VFSDirectory(
        name="shows",
        inode=pyfuse3.InodeT(4),
        parent=VFSRoot(),
    )

    monkeypatch.setattr(vfs, "_get_node_by_path", lambda _: existing_directory)
    create_node = Mock()
    monkeypatch.setattr(vfs, "_get_or_create_node", create_node)
    get_parent_inodes = Mock()
    monkeypatch.setattr(vfs, "_get_parent_inodes", get_parent_inodes)

    result = vfs._register_clean_path(
        clean_path="/shows",
        original_filename="ignored.mkv",
        file_size=1,
        created_at="x",
        updated_at="y",
        entry_type="media",
    )

    assert result is False
    create_node.assert_not_called()
    get_parent_inodes.assert_not_called()
