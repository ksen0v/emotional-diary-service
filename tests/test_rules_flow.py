"""Правила через HTTP: словарь, создание, правка, системные триггеры.

Проверяется то, что обойдут с фронта: недоступная метрика, правило без действий,
правка системного триггера вне разрешённых полей и попытка его выключить.
Последнее проверяется дважды — через API и прямым UPDATE в базу, потому что
ТЗ 6.5 требует запрет, а не договорённость.
"""

import httpx
import pytest
from sqlalchemy import text

from tests.test_identity import csrf, register

pytestmark = pytest.mark.usefixtures("clean_users")

SYSTEM_CODES = ["SR-1", "SR-2", "SR-3", "SR-4"]

TWO_STOPS = {
    "name": "2 стопа подряд по 0.5%",
    "conditions": [{"metric": "loss_streak", "cmp": "ge", "value": 2}],
    "actions": {"alert": True, "lock": {"enabled": True, "minutes": 30}, "buddy": False},
    "unlock": {"timer": True, "review": True, "buddy": False},
}


async def connect_fake(client: httpx.AsyncClient) -> None:
    res = await client.post("/api/v1/source/connections/fake", headers=csrf(client))
    assert res.status_code == 200, res.text


async def rules(client: httpx.AsyncClient) -> dict:
    res = await client.get("/api/v1/rules")
    assert res.status_code == 200, res.text
    return res.json()


async def create(client: httpx.AsyncClient, body: dict) -> httpx.Response:
    return await client.post("/api/v1/rules", headers=csrf(client), json=body)


# --- словарь ---


async def test_metrics_dictionary_follows_active_source(
    app_client: httpx.AsyncClient,
) -> None:
    await register(app_client)
    await connect_fake(app_client)

    res = await app_client.get("/api/v1/rules/metrics")
    assert res.status_code == 200, res.text
    out = res.json()

    assert [m["key"] for m in out["metrics"]] == [
        "loss_streak",
        "drawdown_pct",
        "loss_sum_pct",
    ]
    # Фейк изображает TMM: открытых позиций нет, поэтому честная просадка
    # не вырезана молча, а названа вместе с причиной.
    blocked = {m["key"]: m for m in out["unavailable_metrics"]}
    assert set(blocked) == {"drawdown_full_pct", "unrealized_pct"}
    assert "открытые позиции" in blocked["drawdown_full_pct"]["reason"]

    assert out["significance_pct"] == "0.50"
    assert out["max_conditions"] == 5
    # Доверенного лица ещё нет — экран обязан сказать об этом, а не молча
    # предлагать настройку, которая никому ничего не отправит.
    assert out["buddy_available"] is False
    assert "шаг" in out["buddy_note"]


async def test_significance_threshold_comes_from_settings(
    app_client: httpx.AsyncClient,
) -> None:
    """Порог значимости общий для всех правил и живёт в настройках (ТЗ 6.2)."""
    await register(app_client)
    patched = await app_client.patch(
        "/api/v1/me/settings", headers=csrf(app_client), json={"significance_pct": 0.75}
    )
    assert patched.status_code == 200, patched.text

    out = (await app_client.get("/api/v1/rules/metrics")).json()
    assert out["significance_pct"] == "0.75"


# --- системные триггеры ---


async def test_system_rules_appear_by_themselves(app_client: httpx.AsyncClient) -> None:
    await register(app_client)
    out = await rules(app_client)

    system = [r for r in out["rules"] if r["kind"] == "system"]
    assert [r["system_code"] for r in system] == SYSTEM_CODES
    for rule in system:
        assert rule["enabled"] is True
        # Условия системного правила в коде обработчика, а не в базе.
        assert rule["conditions"] is None
        assert rule["editable_fields"]
        assert rule["fired_last_30d"] == 0
        assert rule["human_text"].startswith("Если ")

    # Экран обязан сказать, что именно уже работает. С шага 10 системные
    # триггеры срабатывают, и флаг поднят — но у SR-1 остаётся своё «ещё не
    # готово», и оно живёт на своём флаге (см. следующие два теста).
    assert out["engine"]["active"] is True
    assert out["engine"]["system_active"] is True


async def test_system_rule_says_what_of_it_does_not_work_yet(
    app_client: httpx.AsyncClient,
) -> None:
    """Долг шага 9: предупреждение живёт на своём флаге, а не на общем.

    На шаге 9 строка «системные триггеры не срабатывают» висела на
    `engine.active`. Движок включился, флаг стал `true` — и строка ушла вместе
    с ним, хотя SR-1…SR-4 так и не срабатывали. Экран замолчал ровно там, где
    должен был говорить, и проверять это пришлось руками.

    Поэтому проверяется не наличие текста, а связь: пока свой флаг не поднят,
    у каждого системного правила есть своё `pending`, и живёт оно на карточке
    рядом со счётчиком — там, где ноль без пояснения читается как «не было».
    """
    from eds.modules.rules import system as sysrules

    await register(app_client)
    out = await rules(app_client)
    engine = out["engine"]

    # Флаги независимы: у каждого «ещё не готово» свой, и поднятие одного
    # не гасит предупреждение другого.
    assert engine["active"] is True
    assert engine["system_active"] is sysrules.SYSTEM_ACTIVE
    assert engine["system_note"]

    system = {r["system_code"]: r for r in out["rules"] if r["kind"] == "system"}
    for code, rule in system.items():
        assert rule["pending"] == sysrules.pending_of(code), code

    # У своих правил такого поля нет: они срабатывают целиком.
    await connect_fake(app_client)
    res = await create(app_client, TWO_STOPS)
    assert res.status_code == 201, res.text
    mine = [r for r in (await rules(app_client))["rules"] if r["kind"] == "user"]
    assert mine and all(r["pending"] is None for r in mine)


async def test_late_tag_has_its_own_flag_on_sr1(
    app_client: httpx.AsyncClient,
) -> None:
    """Когда SR-1 заработает, останется то, что не работает внутри него.

    Ретропроверка позднего тега — шаг 11. Её предупреждение висит на
    `RETRO_ACTIVE`, а не на `SYSTEM_ACTIVE`, иначе включение системных
    триггеров унесёт с экрана и его — ровно та же ошибка, что на шаге 9.
    """
    from eds.modules.rules import system as sysrules

    assert sysrules.SYSTEM_ACTIVE is True
    assert sysrules.RETRO_ACTIVE is False
    assert sysrules.BUDDY_ACTIVE is False
    # У SR-1 своё «ещё не готово» — поздний тег (шаг 11).
    assert sysrules.pending_of(sysrules.SR1) is not None
    # У SR-2 и SR-3 своё — сигнал доверенному лицу (шаг 13). ТЗ 6.5 его им
    # даёт, механика его не делает, и экран обязан сказать об этом сам.
    assert sysrules.pending_of(sysrules.SR2) is not None
    assert sysrules.pending_of(sysrules.SR3) is not None
    # У SR-4 не готово ничего: он работает целиком.
    assert sysrules.pending_of(sysrules.SR4) is None


async def test_system_rules_are_not_duplicated(app_client: httpx.AsyncClient) -> None:
    await register(app_client)
    await rules(app_client)
    await rules(app_client)
    out = await rules(app_client)
    codes = [r["system_code"] for r in out["rules"] if r["kind"] == "system"]
    assert codes == SYSTEM_CODES


async def test_system_rule_cannot_be_disabled_through_api(
    app_client: httpx.AsyncClient,
) -> None:
    await register(app_client)
    out = await rules(app_client)
    sr1 = next(r for r in out["rules"] if r["system_code"] == "SR-1")

    res = await app_client.post(
        f"/api/v1/rules/{sr1['id']}/toggle",
        headers=csrf(app_client),
        json={"enabled": False},
    )
    assert res.status_code == 403, res.text
    assert res.json()["error"]["code"] == "system_rule_always_on"


async def test_system_rule_cannot_be_disabled_in_the_database(
    app_client: httpx.AsyncClient, factory
) -> None:
    """Запрет — ограничение СУБД, а не проверка в коде: код обходится эндпоинтом."""
    await register(app_client)
    await rules(app_client)

    async with factory() as s:
        with pytest.raises(Exception) as err:
            await s.execute(
                text(
                    "UPDATE rules.rules SET enabled = false "
                    "WHERE kind = 'system' AND system_code = 'SR-1'"
                )
            )
            await s.commit()
    assert "нельзя выключить" in str(err.value)


async def test_system_rule_cannot_be_deleted(app_client: httpx.AsyncClient) -> None:
    await register(app_client)
    out = await rules(app_client)
    sr2 = next(r for r in out["rules"] if r["system_code"] == "SR-2")

    res = await app_client.delete(
        f"/api/v1/rules/{sr2['id']}", headers=csrf(app_client)
    )
    assert res.status_code == 403, res.text


async def test_system_rule_editable_fields_are_editable(
    app_client: httpx.AsyncClient,
) -> None:
    await register(app_client)
    out = await rules(app_client)
    sr1 = next(r for r in out["rules"] if r["system_code"] == "SR-1")

    res = await app_client.patch(
        f"/api/v1/rules/{sr1['id']}",
        headers=csrf(app_client),
        json={"unlock": {"timer": False}, "actions": {"lock": {"minutes": 45}}},
    )
    assert res.status_code == 200, res.text
    updated = res.json()
    assert updated["unlock"] == {"timer": False, "review": True, "buddy": False}
    assert updated["actions"]["lock"] == {"enabled": True, "minutes": 45}
    # Частичная правка длительности не должна выключать саму блокировку.
    assert "заблокируется на 45 минут" in updated["human_text"]
    assert "будет заполнен разбор" in updated["human_text"]


async def test_system_rule_conditions_cannot_be_touched(
    app_client: httpx.AsyncClient,
) -> None:
    await register(app_client)
    out = await rules(app_client)
    sr1 = next(r for r in out["rules"] if r["system_code"] == "SR-1")

    res = await app_client.patch(
        f"/api/v1/rules/{sr1['id']}",
        headers=csrf(app_client),
        json={"conditions": [{"metric": "loss_streak", "cmp": "ge", "value": 2}]},
    )
    assert res.status_code == 403, res.text
    assert res.json()["error"]["code"] == "forbidden"


# --- свои правила ---


async def test_create_rule_and_read_it_back(app_client: httpx.AsyncClient) -> None:
    await register(app_client)
    await connect_fake(app_client)

    res = await create(app_client, TWO_STOPS)
    assert res.status_code == 201, res.text
    created = res.json()

    assert created["kind"] == "user"
    assert created["version"] == 1
    assert created["summary"] == "Блокировка 30 мин"
    assert created["human_text"] == (
        "Если за торговый день убыточных сделок подряд не меньше 2 шт — "
        "придёт алерт в Telegram и торговля заблокируется на 30 минут. "
        "Снять можно, когда истечёт таймер и будет заполнен разбор."
    )

    out = await rules(app_client)
    mine = [r for r in out["rules"] if r["kind"] == "user"]
    assert [r["name"] for r in mine] == ["2 стопа подряд по 0.5%"]


async def test_rule_of_three_conditions_reads_as_assembled(
    app_client: httpx.AsyncClient,
) -> None:
    """Главная проверка шага: фраза совпадает с тем, что собрал."""
    await register(app_client)
    await connect_fake(app_client)

    res = await create(
        app_client,
        {
            "name": "Тройное",
            "conditions": [
                {"metric": "loss_streak", "cmp": "ge", "value": 2},
                {"metric": "drawdown_pct", "cmp": "gt", "value": 5, "conn": "and"},
                {"metric": "loss_sum_pct", "cmp": "ge", "value": 1.5, "conn": "or"},
            ],
            "actions": {
                "alert": True,
                "lock": {"enabled": True, "minutes": 60},
                "buddy": False,
            },
            "unlock": {"timer": False, "review": True, "buddy": False},
        },
    )
    assert res.status_code == 201, res.text
    assert res.json()["human_text"] == (
        "Если за торговый день убыточных сделок подряд не меньше 2 шт "
        "и просадка от пика дня больше 5 % депозита "
        "или суммарный убыток не меньше 1.5 % депозита — "
        "придёт алерт в Telegram и торговля заблокируется на 60 минут. "
        "Снять можно, когда будет заполнен разбор."
    )


async def test_unavailable_metric_rejected(app_client: httpx.AsyncClient) -> None:
    await register(app_client)
    await connect_fake(app_client)

    res = await create(
        app_client,
        {
            **TWO_STOPS,
            "conditions": [{"metric": "drawdown_full_pct", "cmp": "ge", "value": 3}],
        },
    )
    assert res.status_code == 422, res.text
    assert res.json()["error"]["code"] == "metric_unavailable"


async def test_rule_without_actions_rejected(app_client: httpx.AsyncClient) -> None:
    await register(app_client)
    res = await create(
        app_client,
        {
            **TWO_STOPS,
            "actions": {
                "alert": False,
                "lock": {"enabled": False, "minutes": None},
                "buddy": False,
            },
        },
    )
    assert res.status_code == 422, res.text
    assert res.json()["error"]["code"] == "no_action"


async def test_buddy_rejected_until_contact_confirmed(
    app_client: httpx.AsyncClient,
) -> None:
    """ТЗ 6.8: без подтверждённого согласия сигнал другу настроить нельзя."""
    await register(app_client)
    res = await create(
        app_client,
        {**TWO_STOPS, "actions": {**TWO_STOPS["actions"], "buddy": True}},
    )
    assert res.status_code == 422, res.text
    assert res.json()["error"]["code"] == "no_confirmed_contact"

    res = await create(
        app_client, {**TWO_STOPS, "unlock": {"timer": True, "review": True, "buddy": True}}
    )
    assert res.status_code == 422, res.text
    assert res.json()["error"]["code"] == "no_confirmed_contact"


async def test_too_many_conditions_rejected(app_client: httpx.AsyncClient) -> None:
    await register(app_client)
    conditions = [{"metric": "loss_streak", "cmp": "ge", "value": 2}] + [
        {"metric": "drawdown_pct", "cmp": "ge", "value": 5, "conn": "and"}
        for _ in range(5)
    ]
    res = await create(app_client, {**TWO_STOPS, "conditions": conditions})
    assert res.status_code == 422, res.text
    assert res.json()["error"]["code"] == "conditions_too_many"


async def test_patch_bumps_version_and_rewrites_sentence(
    app_client: httpx.AsyncClient,
) -> None:
    await register(app_client)
    created = (await create(app_client, TWO_STOPS)).json()

    res = await app_client.patch(
        f"/api/v1/rules/{created['id']}",
        headers=csrf(app_client),
        json={
            "name": "3 стопа подряд",
            "conditions": [{"metric": "loss_streak", "cmp": "ge", "value": 3}],
            "unlock": {"timer": False, "review": False, "buddy": False},
        },
    )
    assert res.status_code == 200, res.text
    updated = res.json()
    assert updated["version"] == 2
    assert updated["name"] == "3 стопа подряд"
    assert "не меньше 3 шт" in updated["human_text"]
    assert updated["human_text"].endswith(
        "Снять её вручную будет нельзя — она сама закончится на границе дня."
    )


async def test_toggle_user_rule_keeps_version(app_client: httpx.AsyncClient) -> None:
    """Выключение — не правка текста, а инциденты ссылаются именно на текст."""
    await register(app_client)
    created = (await create(app_client, TWO_STOPS)).json()

    res = await app_client.post(
        f"/api/v1/rules/{created['id']}/toggle",
        headers=csrf(app_client),
        json={"enabled": False},
    )
    assert res.status_code == 200, res.text
    assert res.json()["enabled"] is False
    assert res.json()["version"] == 1


async def test_deleted_rule_leaves_the_list_but_stays_in_the_table(
    app_client: httpx.AsyncClient, factory
) -> None:
    await register(app_client)
    created = (await create(app_client, TWO_STOPS)).json()

    res = await app_client.delete(
        f"/api/v1/rules/{created['id']}", headers=csrf(app_client)
    )
    assert res.status_code == 204, res.text

    out = await rules(app_client)
    assert [r for r in out["rules"] if r["kind"] == "user"] == []

    async with factory() as s:
        row = await s.execute(
            text("SELECT deleted_at FROM rules.rules WHERE id = :id"),
            {"id": created["id"]},
        )
        # Строка осталась: на правило будут ссылаться инциденты.
        assert row.scalar_one() is not None


async def test_rule_of_another_user_is_not_found(
    app_client: httpx.AsyncClient,
) -> None:
    await register(app_client, email="one@edstest.net")
    created = (await create(app_client, TWO_STOPS)).json()
    await app_client.post("/api/v1/auth/logout", headers=csrf(app_client))

    await register(app_client, email="two@edstest.net")
    res = await app_client.patch(
        f"/api/v1/rules/{created['id']}",
        headers=csrf(app_client),
        json={"name": "чужое"},
    )
    # 404, а не 403: по ответу нельзя узнать, существует ли чужой объект.
    assert res.status_code == 404, res.text


async def test_rules_require_a_session(app_client: httpx.AsyncClient) -> None:
    res = await app_client.get("/api/v1/rules")
    assert res.status_code == 401, res.text


# --- предпросмотр фразы ---


async def test_preview_builds_the_sentence_before_saving(
    app_client: httpx.AsyncClient,
) -> None:
    """Фразу всегда считает сервер — и до «Сохранить» тоже."""
    await register(app_client)
    res = await app_client.post(
        "/api/v1/rules/preview",
        headers=csrf(app_client),
        json={
            "conditions": [{"metric": "drawdown_pct", "cmp": "ge", "value": 5}],
            "actions": {"alert": True, "lock": {"enabled": True, "minutes": 30}},
            "unlock": {"timer": True, "review": False, "buddy": False},
        },
    )
    assert res.status_code == 200, res.text
    out = res.json()
    assert out["valid"] is True
    assert out["problem"] is None
    assert out["human_text"] == (
        "Если за торговый день просадка от пика дня не меньше 5 % депозита — "
        "придёт алерт в Telegram и торговля заблокируется на 30 минут. "
        "Снять можно, когда истечёт таймер."
    )


async def test_preview_explains_why_save_is_off(app_client: httpx.AsyncClient) -> None:
    """Невалидный черновик — всё равно 200: трейдер ещё набирает число."""
    await register(app_client)
    res = await app_client.post(
        "/api/v1/rules/preview",
        headers=csrf(app_client),
        json={
            "conditions": [],
            "actions": {"alert": True, "lock": {"enabled": False}},
            "unlock": {},
        },
    )
    assert res.status_code == 200, res.text
    out = res.json()
    assert out["valid"] is False
    assert out["problem"]["code"] == "conditions_empty"


async def test_preview_of_system_rule_uses_its_own_condition(
    app_client: httpx.AsyncClient,
) -> None:
    await register(app_client)
    res = await app_client.post(
        "/api/v1/rules/preview",
        headers=csrf(app_client),
        json={
            "system_code": "SR-1",
            "conditions": [],
            "actions": {"alert": True, "lock": {"enabled": True, "minutes": None}},
            "unlock": {"timer": False, "review": True, "buddy": False},
        },
    )
    assert res.status_code == 200, res.text
    out = res.json()
    assert out["valid"] is True
    assert out["human_text"] == (
        "Если сделка отмечена как нарушение — придёт алерт в Telegram "
        "и торговля заблокируется до конца торгового дня. "
        # «раньше» — потому что предел у такой блокировки граница дня,
        # а разбор позволяет выйти до неё. Без этого слова «заблокируется
        # до конца дня» и «снять можно» в одной фразе спорят друг с другом.
        "Снять раньше можно, когда будет заполнен разбор."
    )


async def test_phrase_of_day_long_lock_does_not_promise_a_timer(
    app_client: httpx.AsyncClient,
) -> None:
    """Блокировка до конца дня с включённым таймером не обещает досрочный выход.

    У неё `timer_until` не выставляется вовсе, поэтому условие «таймер» не
    выполнится никогда — блокировка кончится на границе дня. Фраза «снять
    можно, когда истечёт таймер» обещала бы обратное, и это худший вид
    вранья: трейдер сидит и ждёт кнопку, которая не загорится.
    """
    await register(app_client)

    async def phrase(unlock: dict) -> str:
        res = await app_client.post(
            "/api/v1/rules/preview",
            headers=csrf(app_client),
            json={
                "system_code": "SR-1",
                "conditions": [],
                "actions": {"alert": False, "lock": {"enabled": True, "minutes": None}},
                "unlock": unlock,
            },
        )
        assert res.status_code == 200, res.text
        return res.json()["human_text"]

    with_timer = await phrase({"timer": True, "review": True, "buddy": False})
    assert "истечёт таймер" not in with_timer
    assert "Снять раньше нельзя" in with_timer
    # Разбор при этом никуда не девается: он требуется, просто не ускоряет.
    assert "Разбор всё равно нужен" in with_timer

    # Без таймера снятие по разбору возможно — и сказано, что оно досрочное.
    without_timer = await phrase({"timer": False, "review": True, "buddy": False})
    assert "Снять раньше можно, когда будет заполнен разбор." in without_timer

    # У блокировки с заданной длительностью таймер остаётся обычным условием.
    res = await app_client.post(
        "/api/v1/rules/preview",
        headers=csrf(app_client),
        json={
            "system_code": "SR-1",
            "conditions": [],
            "actions": {"alert": False, "lock": {"enabled": True, "minutes": 30}},
            "unlock": {"timer": True, "review": True, "buddy": False},
        },
    )
    assert "Снять можно, когда истечёт таймер и будет заполнен разбор." in (
        res.json()["human_text"]
    )


async def test_every_system_rule_can_be_saved(
    app_client: httpx.AsyncClient,
) -> None:
    """Каждый системный триггер должен быть сохраняем как есть.

    SR-2 и SR-3 лежали в базе с `actions.buddy = true`, которого собственная
    валидация не пропускает: подтверждённого контакта нет до шага 13. Правило
    в таком виде невозможно сохранить вообще — кнопка «Сохранить» серая, что
    бы трейдер ни переключил. Проверяется на предпросмотре, потому что именно
    он решает, доступна ли кнопка.
    """
    await register(app_client)
    out = await rules(app_client)

    for rule in (r for r in out["rules"] if r["kind"] == "system"):
        res = await app_client.post(
            "/api/v1/rules/preview",
            headers=csrf(app_client),
            json={
                "system_code": rule["system_code"],
                "conditions": [],
                "actions": rule["actions"],
                "unlock": rule["unlock"],
            },
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["valid"] is True, (rule["system_code"], body["problem"])


async def test_system_rule_declares_only_what_it_does(
    app_client: httpx.AsyncClient,
) -> None:
    """Объявление триггера не обещает того, чего обработчик не делает.

    SR-2 срабатывает, когда блокировка уже идёт, а второй активной быть не
    может — в схеме стоит `one_active_lock`. SR-3 по ТЗ 5.2 и 6.5 блокировку
    не ставит вовсе. Оба раньше объявляли `lock.enabled = true`, и карточка
    обещала блокировку, которой неоткуда взяться.
    """
    await register(app_client)
    by_code = {r["system_code"]: r for r in (await rules(app_client))["rules"]}

    for code in ("SR-2", "SR-3", "SR-4"):
        rule = by_code[code]
        assert rule["actions"]["lock"]["enabled"] is False, code
        # Условия снятия без блокировки ничего не значат: снимать нечего.
        assert not any(rule["unlock"].values()), code
        assert "заблокируется" not in rule["human_text"], code
        assert "Снять" not in rule["human_text"], code

    # У SR-1 блокировка есть, и она названа.
    assert by_code["SR-1"]["actions"]["lock"]["enabled"] is True
    assert "заблокируется до конца торгового дня" in by_code["SR-1"]["human_text"]

    # Сигнал доверенному лицу выключен у всех: контакта нет до шага 13.
    assert all(not r["actions"]["buddy"] for r in by_code.values())
    assert all("уйдёт сигнал" not in r["human_text"] for r in by_code.values())

    # Сгоревший стрик — последствие, а не действие: тумблера у него нет,
    # но во фразе он назван, иначе SR-2 и SR-3 выглядят безобидно.
    assert "стрик за этот день сгорит" in by_code["SR-2"]["human_text"]
    assert "стрик за этот день сгорит" in by_code["SR-3"]["human_text"]
