"""Каталог событий: имена уникальны и совпадают с архитектурой ч.1 §3."""

from eds.contracts import events as ev


def test_no_duplicate_event_names() -> None:
    assert len(ev.ALL) == len(set(ev.ALL))


def test_event_names_are_namespaced() -> None:
    for name in ev.ALL:
        assert "." in name, f"событие без пространства имён: {name}"
        prefix = name.split(".")[0]
        assert prefix in {
            "source",
            "trades",
            "daybook",
            "rules",
            "incidents",
            "streaks",
            "platform",
        }, f"неизвестное пространство имён: {name}"
