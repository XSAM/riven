from types import SimpleNamespace
from unittest.mock import Mock

from routers.secure import scrape


def test_detach_item_if_bound_to_session_expunge_called(monkeypatch):
    session = Mock()
    item = object()

    monkeypatch.setattr(
        scrape, "sa_inspect", lambda _: SimpleNamespace(session=session)
    )

    scrape.detach_item_if_bound_to_session(session, item)

    session.expunge.assert_called_once_with(item)


def test_detach_item_if_bound_to_session_skips_for_detached_item(monkeypatch):
    session = Mock()
    item = object()
    other_session = Mock()

    monkeypatch.setattr(
        scrape, "sa_inspect", lambda _: SimpleNamespace(session=other_session)
    )

    scrape.detach_item_if_bound_to_session(session, item)

    session.expunge.assert_not_called()

