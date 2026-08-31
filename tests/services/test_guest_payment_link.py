"""Платёж с лендинга: привязка к покупателю и видимость в админке.

Фон
---
Гость платит раньше, чем у него появляется аккаунт, поэтому строка платежа
создаётся с ``user_id=None``. Список платежей в админке такие записи молча
выбрасывал (`_build_record` возвращал None), и весь канал продаж с витрины —
включая брошенные и отменённые попытки — в админке отсутствовал.

Тесты фиксируют две половины починки: привязку платежа к покупателю на выдаче
и показ ещё не привязанных платежей как гостевых.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.database.models import PaymentMethod
from app.services.guest_payment_link import link_guest_payment_to_user, resolve_payment_method
from app.services.payment_verification_service import _build_record


class _Result:
    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount


def _db(rowcount: int = 1) -> AsyncMock:
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_Result(rowcount))
    return db


@pytest.mark.parametrize(
    ('raw', 'expected'),
    [
        ('yookassa', PaymentMethod.YOOKASSA),
        ('yookassa_sbp', PaymentMethod.YOOKASSA),
        ('yookassa_card', PaymentMethod.YOOKASSA),
        ('kassa_ai', PaymentMethod.KASSA_AI),  # подчёркивание — часть имени метода
        ('platega_2', PaymentMethod.PLATEGA),
        ('nonsense', None),
        (None, None),
    ],
)
def test_resolve_payment_method(raw: str | None, expected: PaymentMethod | None) -> None:
    assert resolve_payment_method(raw) == expected


@pytest.mark.asyncio
async def test_link_updates_payment_row() -> None:
    db = _db(rowcount=1)

    linked = await link_guest_payment_to_user(
        db, payment_method='yookassa_sbp', purchase_token='tok123', user_id=44
    )

    assert linked is True
    db.execute.assert_awaited_once()
    statement = db.execute.await_args.args[0]
    compiled = statement.compile()
    sql = str(compiled)
    assert 'UPDATE yookassa_payments' in sql
    assert 'metadata_json' in sql, 'ищем платёж по purchase_token из метадаты, а не по внешнему id'
    assert 'user_id IS NULL' in sql, 'чужую привязку перезаписывать нельзя'
    params = set(compiled.params.values())
    assert {'tok123', 'purchase_token', 44} <= params


@pytest.mark.asyncio
async def test_link_skips_methods_without_payment_table() -> None:
    """Покупка с баланса или звёздами — платежа провайдера нет, привязывать нечего."""
    db = _db()

    assert await link_guest_payment_to_user(db, payment_method='balance', purchase_token='t', user_id=1) is False
    assert (
        await link_guest_payment_to_user(db, payment_method='telegram_stars', purchase_token='t', user_id=1) is False
    )
    assert await link_guest_payment_to_user(db, payment_method=None, purchase_token='t', user_id=1) is False
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_link_reports_false_when_nothing_matched() -> None:
    db = _db(rowcount=0)

    linked = await link_guest_payment_to_user(db, payment_method='yookassa', purchase_token='tok', user_id=7)

    assert linked is False


def test_payment_without_user_stays_in_the_list() -> None:
    """Платёж без аккаунта показываем: иначе покупки с витрины не видно вовсе."""
    payment = SimpleNamespace(id=30, user=None, created_at=datetime.now(UTC))

    record = _build_record(
        PaymentMethod.YOOKASSA,
        payment,
        identifier='3227a1b7-000f',
        amount_kopeks=27900,
        status='succeeded',
        is_paid=True,
    )

    assert record is not None
    assert record.user is None
    assert record.amount_kopeks == 27900


def test_payment_without_created_at_is_still_dropped() -> None:
    """А вот запись без даты бесполезна — её отбрасывали и отбрасываем."""
    payment = SimpleNamespace(id=1, user=None, created_at=None)

    assert (
        _build_record(
            PaymentMethod.YOOKASSA, payment, identifier='x', amount_kopeks=100, status='pending', is_paid=False
        )
        is None
    )
