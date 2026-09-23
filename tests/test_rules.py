"""Правило словами и проверка правила — чистые функции, без базы.

Главная проверка шага 8 звучит так: «собрал правило из трёх условий — фраза
внизу совпадает с тем, что собрал». Это проверяется здесь таблицей, а не глазом
на экране: фраза уйдёт в Telegram и в запись инцидента, и ошибка в ней будет
видна в худший момент дня.
"""

import pytest

from eds.modules.rules import dictionary as dic
from eds.modules.rules import human, system, validate
from eds.platform.errors import AppError

TMM = {"provides_tags": True, "provides_positions": False, "provides_balance": False}
BINANCE = {"provides_tags": False, "provides_positions": True, "provides_balance": True}

LOCK_30 = {"alert": True, "lock": {"enabled": True, "minutes": 30}, "buddy": False}
TIMER_REVIEW = {"timer": True, "review": True, "buddy": False}


def cond(metric: str, cmp: str, value, conn: str | None = None) -> dict:
    item = {"metric": metric, "cmp": cmp, "value": value}
    if conn:
        item["conn"] = conn
    return item


# --- фраза ---


def test_sentence_matches_prototype_example() -> None:
    """Правило из прототипа: два стопа подряд, блокировка 30 минут, таймер и разбор."""
    text = human.sentence(
        "за торговый день " + human.conditions_phrase([cond("loss_streak", "ge", 2)]),
        LOCK_30,
        TIMER_REVIEW,
    )
    assert text == (
        "Если за торговый день убыточных сделок подряд не меньше 2 шт — "
        "придёт алерт в Telegram и торговля заблокируется на 30 минут. "
        "Снять можно, когда истечёт таймер и будет заполнен разбор."
    )


def test_sentence_of_three_conditions_keeps_order_and_connectors() -> None:
    items = [
        cond("loss_streak", "ge", 2),
        cond("drawdown_pct", "gt", "5", "and"),
        cond("loss_sum_pct", "ge", "1.5", "or"),
    ]
    assert human.conditions_phrase(items) == (
        "убыточных сделок подряд не меньше 2 шт "
        "и просадка от пика дня больше 5 % депозита "
        "или суммарный убыток не меньше 1.5 % депозита"
    )


def test_sentence_without_actions_says_nothing_will_happen() -> None:
    text = human.sentence(
        "за торговый день убыточных сделок подряд не меньше 2 шт",
        {"alert": False, "lock": {"enabled": False, "minutes": None}, "buddy": False},
        TIMER_REVIEW,
    )
    assert text.endswith("ничего не произойдёт: у правила не выбрано ни одного действия.")


def test_lock_without_unlock_conditions_warns_about_day_boundary() -> None:
    """ТЗ 6.6: ни одного условия — блокировку нельзя снять до границы дня."""
    text = human.sentence(
        "за торговый день просадка от пика дня не меньше 5 % депозита",
        LOCK_30,
        {"timer": False, "review": False, "buddy": False},
    )
    assert text.endswith(
        "Снять её вручную будет нельзя — она сама закончится на границе дня."
    )


def test_unlock_tail_absent_without_lock() -> None:
    """Снимать нечего, если блокировки нет: хвост про снятие не появляется."""
    actions = {"alert": True, "lock": {"enabled": False, "minutes": None}, "buddy": False}
    text = human.sentence(
        "за торговый день убыточных сделок подряд ровно 3 шт", actions, TIMER_REVIEW
    )
    assert "Снять" not in text


def test_three_actions_joined_with_comma_and_and() -> None:
    actions = {"alert": True, "lock": {"enabled": True, "minutes": 60}, "buddy": True}
    parts = human.actions_phrase(actions)
    assert human.join_ru(parts) == (
        "придёт алерт в Telegram, торговля заблокируется на 60 минут "
        "и доверенному лицу уйдёт сигнал"
    )


@pytest.mark.parametrize(
    ("minutes", "tail"),
    [(1, "на 1 минуту"), (2, "на 2 минуты"), (5, "на 5 минут"), (21, "на 21 минуту")],
)
def test_minutes_declension(minutes: int, tail: str) -> None:
    assert human.lock_phrase({"enabled": True, "minutes": minutes}).endswith(tail)


def test_lock_to_end_of_day_phrase() -> None:
    assert (
        human.lock_phrase({"enabled": True, "minutes": None})
        == "торговля заблокируется до конца торгового дня"
    )


@pytest.mark.parametrize(
    ("raw", "shown"), [(2, "2"), ("2", "2"), ("5.0", "5"), ("0.50", "0.5"), ("1.5", "1.5")]
)
def test_numbers_are_shown_as_entered(raw, shown: str) -> None:
    assert human.number(raw) == shown


def test_summary_matches_prototype_cards() -> None:
    assert human.summary(LOCK_30) == "Блокировка 30 мин"
    assert (
        human.summary({"alert": True, "lock": {"enabled": True, "minutes": None}})
        == "Блокировка до конца дня"
    )
    assert (
        human.summary({"alert": True, "lock": {"enabled": False, "minutes": None}})
        == "Только алерт"
    )


# --- словарь метрик ---


def test_tmm_hides_position_metrics_but_names_them() -> None:
    ready, blocked = dic.available(TMM)
    assert [m.key for m in ready] == ["loss_streak", "drawdown_pct", "loss_sum_pct"]
    assert [m.key for m in blocked] == ["drawdown_full_pct", "unrealized_pct"]


def test_binance_opens_position_metrics() -> None:
    ready, blocked = dic.available(BINANCE)
    assert "drawdown_full_pct" in [m.key for m in ready]
    assert blocked == []


def test_without_source_nothing_extra_is_offered() -> None:
    """Пустые возможности — это не «всё доступно»."""
    ready, blocked = dic.available({})
    assert [m.key for m in ready] == ["loss_streak", "drawdown_pct", "loss_sum_pct"]
    assert blocked
    assert dic.reason_for(blocked[0], {}) == dic.NO_SOURCE_REASON


def test_catalog_carries_threshold_and_unlock_conditions() -> None:
    from decimal import Decimal

    out = dic.catalog(TMM, Decimal("0.50"))
    assert out["significance_pct"] == Decimal("0.50")
    assert out["max_conditions"] == 5
    assert [c["key"] for c in out["unlock_conditions"]] == ["timer", "review", "buddy"]
    assert out["unlock_conditions"][0]["params"]["minutes"]["default"] == 30


# --- проверки ---


def test_empty_conditions_rejected() -> None:
    with pytest.raises(AppError) as err:
        validate.conditions([], TMM)
    assert err.value.code == "conditions_empty"


def test_more_than_five_conditions_rejected() -> None:
    items = [cond("loss_streak", "ge", 2)] + [
        cond("drawdown_pct", "ge", 5, "and") for _ in range(5)
    ]
    with pytest.raises(AppError) as err:
        validate.conditions(items, TMM)
    assert err.value.code == "conditions_too_many"


def test_five_conditions_allowed() -> None:
    items = [cond("loss_streak", "ge", 2)] + [
        cond("drawdown_pct", "ge", 5, "and") for _ in range(4)
    ]
    assert len(validate.conditions(items, TMM)["items"]) == 5


def test_first_condition_must_not_have_connector() -> None:
    with pytest.raises(AppError) as err:
        validate.conditions([cond("loss_streak", "ge", 2, "and")], TMM)
    assert err.value.code == "validation_failed"


def test_second_condition_must_have_connector() -> None:
    with pytest.raises(AppError) as err:
        validate.conditions(
            [cond("loss_streak", "ge", 2), cond("drawdown_pct", "ge", 5)], TMM
        )
    assert err.value.code == "validation_failed"


def test_metric_unavailable_for_active_source() -> None:
    with pytest.raises(AppError) as err:
        validate.conditions([cond("drawdown_full_pct", "ge", 5)], TMM)
    assert err.value.code == "metric_unavailable"
    assert "открытые позиции" in err.value.message


def test_value_out_of_range() -> None:
    with pytest.raises(AppError) as err:
        validate.conditions([cond("loss_streak", "ge", 99)], TMM)
    assert err.value.code == "value_out_of_range"


def test_pieces_metric_needs_whole_number() -> None:
    with pytest.raises(AppError) as err:
        validate.conditions([cond("loss_streak", "ge", "2.5")], TMM)
    assert err.value.code == "value_out_of_range"


def test_comma_as_decimal_separator_accepted() -> None:
    out = validate.conditions([cond("drawdown_pct", "ge", "1,5")], TMM)
    assert out["items"][0]["value"] == 1.5


def test_rule_without_actions_rejected() -> None:
    with pytest.raises(AppError) as err:
        validate.actions(
            {"alert": False, "lock": {"enabled": False}, "buddy": False},
            has_confirmed_contact=False,
        )
    assert err.value.code == "no_action"


def test_user_lock_needs_duration() -> None:
    with pytest.raises(AppError) as err:
        validate.actions(
            {"alert": True, "lock": {"enabled": True, "minutes": None}},
            has_confirmed_contact=False,
        )
    assert err.value.code == "validation_failed"


def test_system_lock_may_last_till_day_boundary() -> None:
    out = validate.actions(
        {"alert": True, "lock": {"enabled": True, "minutes": None}},
        has_confirmed_contact=False,
        for_system=True,
    )
    assert out["lock"] == {"enabled": True, "minutes": None}


def test_lock_duration_bounds() -> None:
    with pytest.raises(AppError) as err:
        validate.actions(
            {"alert": True, "lock": {"enabled": True, "minutes": 1}},
            has_confirmed_contact=False,
        )
    assert err.value.code == "value_out_of_range"


def test_buddy_needs_confirmed_contact() -> None:
    with pytest.raises(AppError) as err:
        validate.actions(
            {"alert": True, "lock": {"enabled": False}, "buddy": True},
            has_confirmed_contact=False,
        )
    assert err.value.code == "no_confirmed_contact"

    with pytest.raises(AppError) as err:
        validate.unlock({"buddy": True}, has_confirmed_contact=False)
    assert err.value.code == "no_confirmed_contact"


def test_buddy_allowed_once_contact_confirmed() -> None:
    out = validate.actions(
        {"alert": True, "lock": {"enabled": False}, "buddy": True},
        has_confirmed_contact=True,
    )
    assert out["buddy"] is True


def test_empty_name_rejected() -> None:
    with pytest.raises(AppError):
        validate.name("   ")


# --- системные правила ---


def test_system_patch_allows_only_listed_fields() -> None:
    validate.system_patch("SR-1", {"unlock": {"timer": False}})
    validate.system_patch("SR-1", {"actions": {"lock": {"minutes": 45}}})
    with pytest.raises(AppError) as err:
        validate.system_patch("SR-1", {"conditions": [cond("loss_streak", "ge", 2)]})
    assert err.value.code == "forbidden"
    assert err.value.http_status == 403


def test_sr3_duration_is_not_editable() -> None:
    """День без допуска закрыт целиком (ТЗ 5.2) — «на 30 минут» здесь бессмысленно."""
    with pytest.raises(AppError) as err:
        validate.system_patch("SR-3", {"actions": {"lock": {"minutes": 30}}})
    assert err.value.code == "forbidden"


def test_every_system_rule_has_a_sentence() -> None:
    for rule in system.SYSTEM_RULES:
        text = human.sentence(rule.if_text(rule.actions), rule.actions, rule.unlock)
        assert text.startswith("Если ")
        assert "{minutes}" not in text


def test_sr4_sentence_carries_configured_delay() -> None:
    rule = system.BY_CODE["SR-4"]
    actions = {**rule.actions, "remind_after_minutes": 7}
    assert rule.if_text(actions) == "сделка закрыта и не размечена дольше 7 минут"
