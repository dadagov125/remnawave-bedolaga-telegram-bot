"""Гостевая покупка по номеру телефона: выдача не должна коммитить сама.

Фон — баг из прода (InvalidRequestError: Can't operate on closed transaction)
----------------------------------------------------------------------------
`fulfill_purchase` держит `SELECT ... FOR UPDATE` на строке покупки и создаёт
пользователя внутри `begin_nested()`. Телефонная ветка звала общий
`create_user_by_phone`, который коммитил сам: коммит закрывал и транзакцию, и
SAVEPOINT, после чего следующий же запрос (`refresh`) падал. Покупатель платил
и оставался без подписки до ретрая — до часа ожидания.

Тесты фиксируют инвариант: в гостевой покупке пользователь создаётся во
внешней транзакции (только flush), а вход по звонку, где функция сама себе
транзакция, продолжает коммитить.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.database.crud.user import create_user_by_phone
from app.database.models import PromoGroup
from app.services.guest_purchase_service import _find_or_create_user


def _empty_result() -> SimpleNamespace:
    """Мимикрия под `db.execute(...).scalar_one_or_none() -> None`."""
    return SimpleNamespace(
        scalars=lambda: SimpleNamespace(first=lambda: None),
        scalar_one_or_none=lambda: None,
    )


def _async_nested_ctx() -> MagicMock:
    """Асинхронный контекст-менеджер для `db.begin_nested()`."""
    nested = MagicMock()
    nested.__aenter__ = AsyncMock(return_value=None)
    nested.__aexit__ = AsyncMock(return_value=None)
    return nested


def _session() -> AsyncMock:
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_empty_result())
    db.begin_nested = MagicMock(return_value=_async_nested_ctx())
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    # `db.add` на AsyncSession синхронный
    db.add = MagicMock()
    return db


def _patched_crud(promo_group: object, referral_code: str = 'PHONEREF'):
    """Группу по умолчанию патчим в обоих пространствах имён.

    Её разрешает сервис (общий хелпер для почты и телефона), а `create_user_by_phone`
    падает в неё же, когда группу не передали.
    """
    return (
        patch(
            'app.services.guest_purchase_service._get_or_create_default_promo_group',
            AsyncMock(return_value=promo_group),
        ),
        patch(
            'app.database.crud.user._get_or_create_default_promo_group',
            AsyncMock(return_value=promo_group),
        ),
        patch(
            'app.database.crud.user.create_unique_referral_code',
            AsyncMock(return_value=referral_code),
        ),
    )


@pytest.mark.asyncio
async def test_phone_guest_purchase_does_not_commit() -> None:
    """Покупка с лендинга по телефону создаёт пользователя без своего коммита.

    Коммит здесь закрывал транзакцию вызывающего вместе с SAVEPOINT'ом, и
    выдача падала с InvalidRequestError.
    """
    db = _session()
    # Настоящая модель, а не заглушка: `user.promo_group = ...` — это relationship,
    # SQLAlchemy требует ORM-объект.
    promo_group = PromoGroup(id=1, name='Базовый юзер', is_default=True)
    service_group_patch, crud_group_patch, code_patch = _patched_crud(promo_group)

    with service_group_patch, crud_group_patch, code_patch:
        user, is_new = await _find_or_create_user(db, 'phone', '+79991234567')

    assert is_new is True
    assert user.phone == '+79991234567'
    assert user.phone_verified is False, 'номер из формы не подтверждён — подтверждение будет звонком при входе'
    assert user.referral_code == 'PHONEREF'
    db.add.assert_called_once_with(user)
    db.flush.assert_awaited()
    db.commit.assert_not_awaited(), 'коммит принадлежит fulfill_purchase, иначе рвётся его транзакция и FOR UPDATE'
    db.refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_phone_login_still_commits() -> None:
    """Вход по звонку остаётся самодостаточным: там функция и есть транзакция."""
    db = _session()
    # Настоящая модель, а не заглушка: `user.promo_group = ...` — это relationship,
    # SQLAlchemy требует ORM-объект.
    promo_group = PromoGroup(id=1, name='Базовый юзер', is_default=True)
    service_group_patch, crud_group_patch, code_patch = _patched_crud(promo_group)

    with service_group_patch, crud_group_patch, code_patch:
        user = await create_user_by_phone(db, '+79991234567')

    assert user.phone_verified is True
    db.commit.assert_awaited_once()
    db.refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_phone_purchase_uses_tariff_promo_group() -> None:
    """Тариф, проданный отдельной группе, кладёт покупателя в неё — как и по почте.

    Раньше группу тарифа умела разрешать только почтовая ветка, а покупатель по
    телефону попадал в дефолтную: другие скидки и риск не увидеть в кабинете
    тариф, который только что купил.
    """
    db = _session()
    closed_group = PromoGroup(id=9, name='Партнёрская')
    default_group = PromoGroup(id=1, name='Базовый юзер', is_default=True)
    tariff = SimpleNamespace(allowed_promo_groups=[closed_group])

    with (
        patch('app.services.guest_purchase_service.get_tariff_by_id', AsyncMock(return_value=tariff)),
        patch(
            'app.services.guest_purchase_service._get_or_create_default_promo_group',
            AsyncMock(return_value=default_group),
        ),
        patch('app.database.crud.user.create_unique_referral_code', AsyncMock(return_value='REF00001')),
    ):
        user, _ = await _find_or_create_user(db, 'phone', '+79991234567', tariff_id=3)

    assert user.promo_group_id == closed_group.id


@pytest.mark.asyncio
async def test_phone_purchase_falls_back_to_default_group() -> None:
    """Тариф без своей группы — дефолтная, как и было."""
    db = _session()
    default_group = PromoGroup(id=1, name='Базовый юзер', is_default=True)
    tariff = SimpleNamespace(allowed_promo_groups=[])

    with (
        patch('app.services.guest_purchase_service.get_tariff_by_id', AsyncMock(return_value=tariff)),
        patch(
            'app.services.guest_purchase_service._get_or_create_default_promo_group',
            AsyncMock(return_value=default_group),
        ),
        patch('app.database.crud.user.create_unique_referral_code', AsyncMock(return_value='REF00002')),
    ):
        user, _ = await _find_or_create_user(db, 'phone', '+79991234567', tariff_id=3)

    assert user.promo_group_id == default_group.id


@pytest.mark.asyncio
async def test_both_channels_resolve_the_same_group() -> None:
    """Почта и телефон разрешают группу одним хелпером — расходиться им больше нечем."""
    closed_group = PromoGroup(id=9, name='Партнёрская')
    default_group = PromoGroup(id=1, name='Базовый юзер', is_default=True)
    tariff = SimpleNamespace(allowed_promo_groups=[closed_group])

    groups: list[int] = []
    for contact_type, contact_value in (('phone', '+79991234567'), ('email', 'buyer@example.com')):
        db = _session()
        with (
            patch('app.services.guest_purchase_service.get_tariff_by_id', AsyncMock(return_value=tariff)),
            patch(
                'app.services.guest_purchase_service._get_or_create_default_promo_group',
                AsyncMock(return_value=default_group),
            ),
            patch(
                'app.services.guest_purchase_service.create_unique_referral_code',
                AsyncMock(return_value='REF00003'),
            ),
            patch('app.database.crud.user.create_unique_referral_code', AsyncMock(return_value='REF00003')),
        ):
            user, _ = await _find_or_create_user(db, contact_type, contact_value, tariff_id=3)
        groups.append(user.promo_group_id)

    assert groups[0] == groups[1] == closed_group.id
