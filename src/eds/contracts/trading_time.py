"""Торговый день и его границы. Общее правило, а не свойство одного модуля.

Живёт в контрактах по необходимости: по этому правилу `trades` присваивает
сделке день, `daybook` открывает и закрывает сессию, а оркестрация считает,
когда день кончится. Будь оно в одном из модулей, остальным пришлось бы либо
импортировать чужой модуль, либо повторить его у себя — и при первой правке
границы дня расчёты разъехались бы между модулями.
"""

import datetime as dt
import zoneinfo


def trading_day(moment: dt.datetime, timezone: str, cutoff: dt.time) -> dt.date:
    """Торговый день момента, в таймзоне трейдера.

    Граница дня сдвигает сутки: при границе 03:00 момент 01:30 относится
    к предыдущему торговому дню. Так ночная торговля не разрывается на два дня
    посередине сессии.

    У сделки день считается по времени ОТКРЫТИЯ и фиксируется при приёме:
    при смене таймзоны история не пересчитывается (решение Архитектуры ч.1).
    """
    local = moment.astimezone(zoneinfo.ZoneInfo(timezone))
    day = local.date()
    if local.time() < cutoff:
        day = day - dt.timedelta(days=1)
    return day


def day_bounds(
    day: dt.date, timezone: str, cutoff: dt.time
) -> tuple[dt.datetime, dt.datetime]:
    """Начало и конец торгового дня в UTC. Конец — исключающий, это следующая граница.

    Переход на летнее время делает один день короче или длиннее на час, и это
    правильно: граница задана местным временем трейдера, а не длительностью суток.
    """
    tz = zoneinfo.ZoneInfo(timezone)
    start = dt.datetime.combine(day, cutoff, tzinfo=tz)
    end = dt.datetime.combine(day + dt.timedelta(days=1), cutoff, tzinfo=tz)
    return start.astimezone(dt.UTC), end.astimezone(dt.UTC)


def day_ends_at(day: dt.date, timezone: str, cutoff: dt.time) -> dt.datetime:
    """Последняя секунда торгового дня — для таймеров в интерфейсе.

    Не следующая граница, а секунда до неё: иначе на экране «день кончается
    в 00:00» выглядело бы как начало нового дня.
    """
    _, end = day_bounds(day, timezone, cutoff)
    return end - dt.timedelta(seconds=1)
