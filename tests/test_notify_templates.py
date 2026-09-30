"""Тексты уведомлений: что проверяется, что не проверяется и что запрещено.

Правило одно и оно из ТЗ 9.5: **что трейдер написал, то и придёт.** Сервис
проверяет только техническую корректность — известные подстановки, целые
скобки, непустую строку до 500 символов. На осмысленность не проверяет и на
стандартный текст не откатывается.

Жёсткое исключение ровно одно: **в сообщениях доверенному лицу сумм нет** —
ни подстановкой, ни вручную. Друг видит факт, а не финансы.
"""

import pytest

from eds.app import notify as app_notify
from eds.modules.notifications import templates
from eds.platform.errors import AppError


def check(key: str, body: str) -> AppError:
    with pytest.raises(AppError) as err:
        templates.validate(key, body)
    return err.value


# --- каталог ---


def test_catalog_matches_the_prototype() -> None:
    """Состав и дефолты — из прототипа `NotifyTexts.dc.html` дословно.

    Это то, что видит трейдер с первой минуты после регистрации, ничего
    не настраивая. Для большинства дефолт и есть голос сервиса, поэтому
    он написан как готовый продукт, а не как заглушка.
    """
    from_prototype = [
        "violation_detected",
        "lock_started",
        "lock_lifted",
        "lock_breached",
        "no_admission",
        "retro_violation",
        "streak_broken",
        "unmarked_reminder",
        "sync_lost",
        "buddy_signal",
        "buddy_confirm_request",
    ]
    keys = [t.key for t in templates.CATALOG]
    assert set(from_prototype) <= set(keys)

    assert (
        templates.BY_KEY["lock_started"].default
        == "Сработало: {rule_name}. Блокировка до {until}."
    )
    assert (
        templates.BY_KEY["sync_lost"].default
        == "Синхронизация молчит {minutes} минут. Правила сейчас не работают."
    )
    # Этот текст несёт больше работы, чем остальные: он говорит прямо, что
    # правила не работают. Смягчить его — значит оставить трейдера уверенным,
    # что блокировки его прикрывают, когда поток умер два часа назад.
    assert "не работают" in templates.BY_KEY["sync_lost"].default
    # А этот содержит условие, а не просьбу: без него друг нажмёт кнопку
    # из вежливости, и внешний контроль превратится в формальность.
    assert "только если поговорил" in templates.BY_KEY["buddy_confirm_request"].default


def test_added_key_is_named_and_needed() -> None:
    """Двенадцатый ключ — отступление от прототипа, и вот зачем оно.

    ТЗ 6.4 даёт правилу действие «алерт трейдеру» отдельно от блокировки,
    и фраза правила обещает алерт. Правило только с алертом блокировку
    не начинает, значит ни один из одиннадцати текстов прототипа ему
    не подходит — и обещание осталось бы невыполненным.
    """
    assert "rule_fired" in templates.BY_KEY
    assert templates.BY_KEY["rule_fired"].default == "Сработало: {rule_name}."


# --- проверки ---


def test_unknown_placeholder_is_refused_with_the_available_list() -> None:
    err = check("lock_started", "Блокировка до {pnl}.")
    assert err.code == "unknown_placeholder"
    assert err.details["unknown"] == ["pnl"]
    assert "rule_name" in err.details["available"]


def test_broken_braces_are_refused() -> None:
    """Проверяется ровно то, что шаблон технически отрендерится."""
    assert check("lock_started", "Блокировка до {until.").code == "malformed_template"
    assert check("lock_started", "Блокировка до until}.").code == "malformed_template"
    assert check("lock_started", "Блокировка {} сейчас").code == "malformed_template"


def test_empty_and_too_long_are_refused() -> None:
    assert check("lock_started", "   ").code == "validation_failed"
    assert check("lock_started", "x" * 501).code == "validation_failed"
    assert templates.validate("lock_started", "x" * 500)


def test_short_and_rude_text_is_accepted_as_is() -> None:
    """«Хватит.» — отличный текст, который отсеяла бы любая эвристика.

    Никаких проверок на осмысленность, никакого отката на дефолт: это
    сообщение человека самому себе, и сервис в него не вмешивается.
    """
    assert templates.validate("lock_started", "Хватит.") == "Хватит."
    assert templates.validate("lock_started", "Два стопа. Хватит. До {until}.")


def test_money_is_forbidden_for_the_buddy_and_the_reason_is_named() -> None:
    """Жёсткое ограничение ТЗ 9.5. Отдельный код ошибки, а не общий.

    Причина отказа здесь другая, чем у неизвестной подстановки, и трейдер
    должен прочитать именно её: друг видит факт, а не финансы.
    """
    err = check("buddy_signal", "{trader_name} слил {profit_usd}.")
    assert err.code == "forbidden_placeholder"
    assert err.details["forbidden"] == ["profit_usd"]
    assert "сумм нет" in err.message

    # И те, которых нет в каталоге вовсе: трейдер впишет «{pnl}» с той же
    # вероятностью, что «{profit_usd}».
    assert check("buddy_signal", "Он потерял {pnl}").code == "forbidden_placeholder"
    assert check("buddy_confirm_request", "{equity_pct}").code == "forbidden_placeholder"


def test_money_is_allowed_for_the_trader_himself() -> None:
    """Запрет — про сообщения другу, а не про уведомления себе."""
    assert templates.validate(
        "violation_detected", "Сделка {symbol}: {profit_usd}."
    )


def test_buddy_templates_offer_no_money_chips() -> None:
    """Ограничение стоит и в списке подстановок, а не только в проверке.

    Иначе экран предлагал бы чип, который сервер отклонит, — и трейдер
    узнал бы о запрете методом тыка.
    """
    for template in templates.CATALOG:
        if not template.buddy:
            continue
        assert not (set(template.placeholders) & templates.MONEY_PLACEHOLDERS)


# --- подстановка ---


def test_render_puts_values_in() -> None:
    text = templates.render(
        templates.BY_KEY["lock_started"].default,
        {"rule_name": "2 стопа подряд", "until": "15:12"},
    )
    assert text == "Сработало: 2 стопа подряд. Блокировка до 15:12."


def test_render_falls_on_a_missing_value() -> None:
    """Ветки «если текст плохой» у рендера нет.

    Он либо подставляет и отправляет, либо падает на подстановке, для которой
    не дали значения. Такой шаблон в базу не попадёт — валидация не пустит, —
    поэтому падение здесь означает ошибку кода, а не трейдера.
    """
    with pytest.raises(templates.PlaceholderMissing):
        templates.render("До {until}", {})


def test_preview_uses_the_prototype_samples() -> None:
    rendered, sample = templates.preview("Два стопа. Хватит. До {until}.")
    assert rendered == "Два стопа. Хватит. До 15:12."
    assert sample == {"until": "15:12"}


# --- у каждого текста есть отправитель ---


def test_every_template_has_a_producer() -> None:
    """Шаблон без отправителя — обещание, которого сервис не выполняет.

    Трейдер видит событие в списке, переписывает текст — и никогда его
    не получает. Поэтому список ключей и список отправителей сверяются
    тестом, а не памятью.
    """
    produced = {
        "violation_detected",
        "rule_fired",
        "lock_started",
        "lock_lifted",
        "lock_breached",
        "no_admission",
        "retro_violation",
        "streak_broken",
        "unmarked_reminder",
        "sync_lost",
        "buddy_signal",
        "buddy_confirm_request",
    }
    assert produced == set(templates.BY_KEY)


def test_producers_cover_every_placeholder() -> None:
    """Значения даёт тот, кто ставит уведомление в очередь.

    Список подстановок и то, что реально подставляется, живут в разных
    файлах и разъезжаются молча: пропущенное значение видно только в момент
    отправки, то есть в худший момент.
    """
    values = {
        "violation_detected": {
            "symbol": "BTCUSDT",
            "open_time": "14:31",
            "profit_usd": app_notify.money(-84.2),
            "day": "19 сентября",
        },
        "rule_fired": {"rule_name": "2 стопа подряд", "day": "19 сентября"},
        "lock_started": {
            "rule_name": "2 стопа подряд",
            "minutes": 30,
            "until": "15:12",
            "day": "19 сентября",
        },
        "lock_lifted": {
            "rule_name": "2 стопа подряд",
            "reason": "условия выполнены",
            "day": "19 сентября",
        },
        "lock_breached": {
            "symbol": "BTCUSDT",
            "open_time": "14:31",
            "rule_name": "2 стопа подряд",
        },
        "no_admission": {"score": 11, "min_score": 13, "day": "19 сентября"},
        "retro_violation": {
            "day": "19 сентября",
            "streak_before": 14,
            "streak_after": 1,
        },
        "streak_broken": {
            "streak_before": 14,
            "reason": "нарушение",
            "day": "19 сентября",
        },
        "unmarked_reminder": {"count": 3, "minutes": 15},
        "sync_lost": {"minutes": 7},
        "buddy_signal": {
            "trader_name": "Владислав",
            "rule_name": "2 стопа подряд",
            "day": "19 сентября",
        },
        "buddy_confirm_request": {
            "trader_name": "Владислав",
            "rule_name": "2 стопа подряд",
        },
    }
    for template in templates.CATALOG:
        given = values[template.key]
        missing = set(template.placeholders) - set(given)
        assert not missing, (template.key, missing)
        # И дефолт, и любой текст из разрешённых подстановок обязан
        # отрендериться этими значениями.
        templates.render(template.default, given)
        templates.render(
            " ".join("{" + name + "}" for name in template.placeholders), given
        )


def test_money_is_formatted_like_on_the_screen() -> None:
    """Знак перед долларом: «$-84.20» читается как сломанная вёрстка.

    Формат тот же, что на экране: уведомление и лента говорят об одной
    сделке, и числа в них должны выглядеть одинаково.
    """
    assert app_notify.money(-84.2) == "−$84.20"
    assert app_notify.money(140.1) == "+$140.10"
    assert app_notify.money(0) == "$0.00"
