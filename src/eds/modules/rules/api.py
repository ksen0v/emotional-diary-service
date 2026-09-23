"""HTTP модуля rules: словарь метрик и CRUD правил (Архитектура ч.2 §3.6)."""

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from eds.modules.rules import service
from eds.platform import auth, db

router = APIRouter(prefix="/api/v1", tags=["rules"])


class ConditionIn(BaseModel):
    metric: str
    cmp: str
    value: Decimal
    conn: str | None = None


class LockIn(BaseModel):
    enabled: bool = False
    minutes: int | None = None


class ActionsIn(BaseModel):
    alert: bool = False
    lock: LockIn = Field(default_factory=LockIn)
    buddy: bool = False


class UnlockIn(BaseModel):
    timer: bool = False
    review: bool = False
    buddy: bool = False


class RuleIn(BaseModel):
    name: str
    conditions: list[ConditionIn]
    actions: ActionsIn
    unlock: UnlockIn


class RulePatchIn(BaseModel):
    """Частичная правка: любое подмножество полей.

    У системного правила разрешённые поля перечислены в `editable_fields`
    того же правила, и попытка тронуть остальное даёт 403, а не тихо проходит.
    """

    name: str | None = None
    conditions: list[ConditionIn] | None = None
    actions: dict[str, Any] | None = None
    unlock: dict[str, bool] | None = None


class PreviewIn(BaseModel):
    """Черновик правила для предпросмотра фразы. Названия здесь нет: оно во фразу
    не входит, а требовать его на каждый удар по клавише незачем."""

    system_code: str | None = None
    conditions: list[ConditionIn] = Field(default_factory=list)
    actions: dict[str, Any] = Field(default_factory=dict)
    unlock: dict[str, bool] = Field(default_factory=dict)


class PreviewOut(BaseModel):
    if_text: str
    human_text: str
    summary: str
    valid: bool
    problem: dict[str, Any] | None


class ToggleIn(BaseModel):
    enabled: bool


class RuleOut(BaseModel):
    id: str
    name: str
    kind: str
    system_code: str | None
    enabled: bool
    conditions: dict[str, Any] | None
    actions: dict[str, Any]
    unlock: dict[str, Any]
    # То, что стоит после «Если»: у системного правила это фиксированное
    # описание из кода, и экран показывает его текстом, а не селектами.
    if_text: str
    human_text: str
    summary: str
    fired_last_30d: int
    version: int
    updated_at: dt.datetime
    editable_fields: list[str] | None = None


class RulesOut(BaseModel):
    rules: list[RuleOut]
    unavailable_metrics: list[str]
    engine: dict[str, Any]


class MetricOut(BaseModel):
    key: str
    name: str
    unit: str
    type: str
    min: Decimal
    max: Decimal
    hint: str | None = None


class UnavailableMetricOut(BaseModel):
    key: str
    name: str
    unit: str
    requires: str | None
    reason: str


class NamedOut(BaseModel):
    key: str
    name: str


class MetricsOut(BaseModel):
    metrics: list[MetricOut]
    unavailable_metrics: list[UnavailableMetricOut]
    comparators: list[NamedOut]
    connectors: list[NamedOut]
    significance_pct: Decimal
    max_conditions: int
    unlock_conditions: list[dict[str, Any]]
    buddy_available: bool
    buddy_note: str


def _conditions(items: list[ConditionIn]) -> list[dict[str, Any]]:
    return [item.model_dump(exclude_none=False) for item in items]


@router.get("/rules/metrics", response_model=MetricsOut)
async def metrics(
    user: auth.CurrentUser = Depends(auth.current_user),
    prefs: auth.UserPrefs = Depends(auth.current_prefs),
    s: AsyncSession = Depends(db.session),
) -> MetricsOut:
    """Из чего можно собрать правило при текущем активном источнике.

    Возможности источника спрашиваем через платформу: модуль rules не должен
    знать, что модуль source существует.
    """
    caps = await auth.source_capabilities(s, user.user_id)
    return MetricsOut(**service.metrics_catalog(caps, prefs.significance_pct))


@router.get("/rules", response_model=RulesOut)
async def rules(
    user: auth.CurrentUser = Depends(auth.current_user),
    s: AsyncSession = Depends(db.session),
) -> RulesOut:
    caps = await auth.source_capabilities(s, user.user_id)
    data = await service.listing(s, user.user_id, caps)
    # Чтение, которое пишет: досоздаёт системные правила, если их нет.
    await s.commit()
    return RulesOut(**data)


@router.post("/rules", response_model=RuleOut, status_code=201)
async def create_rule(
    body: RuleIn,
    user: auth.CurrentUser = Depends(auth.current_user),
    _: None = Depends(auth.check_csrf),
    s: AsyncSession = Depends(db.session),
) -> RuleOut:
    caps = await auth.source_capabilities(s, user.user_id)
    row = await service.create(
        s,
        user.user_id,
        name=body.name,
        conditions=_conditions(body.conditions),
        actions=body.actions.model_dump(),
        unlock=body.unlock.model_dump(),
        capabilities=caps,
    )
    await s.commit()
    return RuleOut(**service.rule_out(row))


@router.post("/rules/preview", response_model=PreviewOut)
async def preview_rule(
    body: PreviewIn,
    user: auth.CurrentUser = Depends(auth.current_user),
    _: None = Depends(auth.check_csrf),
    s: AsyncSession = Depends(db.session),
) -> PreviewOut:
    """Правило словами до сохранения. Фразу всегда считает сервер."""
    caps = await auth.source_capabilities(s, user.user_id)
    return PreviewOut(
        **service.preview(
            capabilities=caps,
            system_code=body.system_code,
            conditions=_conditions(body.conditions),
            actions=body.actions,
            unlock=body.unlock,
        )
    )


@router.patch("/rules/{rule_id}", response_model=RuleOut)
async def patch_rule(
    rule_id: uuid.UUID,
    body: RulePatchIn,
    user: auth.CurrentUser = Depends(auth.current_user),
    _: None = Depends(auth.check_csrf),
    s: AsyncSession = Depends(db.session),
) -> RuleOut:
    caps = await auth.source_capabilities(s, user.user_id)
    patch_body: dict[str, Any] = {}
    if body.name is not None:
        patch_body["name"] = body.name
    if body.conditions is not None:
        patch_body["conditions"] = _conditions(body.conditions)
    if body.actions is not None:
        patch_body["actions"] = body.actions
    if body.unlock is not None:
        patch_body["unlock"] = body.unlock

    row = await service.patch(s, user.user_id, rule_id, patch_body, caps)
    await s.commit()
    return RuleOut(**service.rule_out(row))


@router.post("/rules/{rule_id}/toggle", response_model=RuleOut)
async def toggle_rule(
    rule_id: uuid.UUID,
    body: ToggleIn,
    user: auth.CurrentUser = Depends(auth.current_user),
    _: None = Depends(auth.check_csrf),
    s: AsyncSession = Depends(db.session),
) -> RuleOut:
    row = await service.toggle(s, user.user_id, rule_id, body.enabled)
    await s.commit()
    return RuleOut(**service.rule_out(row))


@router.delete("/rules/{rule_id}", status_code=204)
async def delete_rule(
    rule_id: uuid.UUID,
    user: auth.CurrentUser = Depends(auth.current_user),
    _: None = Depends(auth.check_csrf),
    s: AsyncSession = Depends(db.session),
) -> Response:
    """Правило помечается удалённым, но не исчезает: на него ссылаются инциденты."""
    await service.delete(s, user.user_id, rule_id)
    await s.commit()
    return Response(status_code=204)
