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


def test_every_adapter_implements_the_port() -> None:
    """У всех источников есть все методы порта, и они лежат в классе.

    Проверка не формальная. `TradeSource` — это `Protocol`, а он не
    выполняется в рантайме: метод, дописанный в конец файла мимо класса,
    проходит и линтер, и типизацию, а падает только в тот момент, когда
    оркестрация его вызовет. На шаге 12 так и случилось — и поймал это
    не тест, а снимок экрана.
    """
    import inspect

    from eds.modules.source.adapters.binance.source import BinanceSource
    from eds.modules.source.adapters.fake.source import FakeSource
    from eds.modules.source.adapters.tmm.source import TmmSource

    required = [
        "capabilities",
        "fetch_accounts",
        "fetch_tags",
        "fetch_trades",
        "fetch_positions",
        "fetch_balance",
    ]
    missing: list[str] = []
    for cls in (FakeSource, TmmSource, BinanceSource):
        for name in required:
            member = inspect.getattr_static(cls, name, None)
            if member is None or not callable(member):
                missing.append(f"{cls.__name__}.{name}")
        if getattr(cls, "provider", None) is None:
            missing.append(f"{cls.__name__}.provider")
    assert not missing, "у источников нет методов порта: " + ", ".join(missing)
