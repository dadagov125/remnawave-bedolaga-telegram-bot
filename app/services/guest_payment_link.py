"""Привязка платежа с лендинга к покупателю.

Гость платит раньше, чем у него появляется аккаунт: платёж в шлюзе создаётся с
``user_id=None`` (см. ``PaymentService.create_guest_payment``). Аккаунт заводится
только на выдаче, и без обратной привязки платёж остаётся ничьим — админский
список платежей такие записи не показывает, в карточке пользователя их тоже нет.

Ищем строку платежа по ``purchase_token`` из метадаты, а не по внешнему id: токен
кладётся в метадату любого гостевого платежа одинаково, а внешние идентификаторы у
провайдеров называются по-разному (``order_id``, ``uuid``, ``bill_id``…) и хранят
то одно, то другое.
"""

from __future__ import annotations

import structlog
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    AntilopayPayment,
    AuraPayPayment,
    CisPayPayment,
    CloudPaymentsPayment,
    CryptoBotPayment,
    DonutPayment,
    EtoplatezhiPayment,
    FreekassaPayment,
    HeleketPayment,
    JupiterPayment,
    KassaAiPayment,
    LavaPayment,
    MulenPayPayment,
    OverpayPayment,
    Pal24Payment,
    PaymentMethod,
    PayPearPayment,
    PlategaPayment,
    RioPayPayment,
    RollyPayPayment,
    SeverPayPayment,
    WataPayment,
    YooKassaPayment,
)


logger = structlog.get_logger(__name__)

# Метод → таблица платежей. Тот же набор, что перебирает поиск платежей в
# админке (``payment_search_service``); методы без своей таблицы (звёзды,
# tribute, баланс, ручное зачисление) сюда не входят — привязывать нечего.
_PAYMENT_MODELS: dict[PaymentMethod, type] = {
    PaymentMethod.YOOKASSA: YooKassaPayment,
    PaymentMethod.CRYPTOBOT: CryptoBotPayment,
    PaymentMethod.HELEKET: HeleketPayment,
    PaymentMethod.MULENPAY: MulenPayPayment,
    PaymentMethod.PAL24: Pal24Payment,
    PaymentMethod.WATA: WataPayment,
    PaymentMethod.PLATEGA: PlategaPayment,
    PaymentMethod.CLOUDPAYMENTS: CloudPaymentsPayment,
    PaymentMethod.FREEKASSA: FreekassaPayment,
    PaymentMethod.KASSA_AI: KassaAiPayment,
    PaymentMethod.RIOPAY: RioPayPayment,
    PaymentMethod.SEVERPAY: SeverPayPayment,
    PaymentMethod.OVERPAY: OverpayPayment,
    PaymentMethod.PAYPEAR: PayPearPayment,
    PaymentMethod.ROLLYPAY: RollyPayPayment,
    PaymentMethod.AURAPAY: AuraPayPayment,
    PaymentMethod.ETOPLATEZHI: EtoplatezhiPayment,
    PaymentMethod.ANTILOPAY: AntilopayPayment,
    PaymentMethod.JUPITER: JupiterPayment,
    PaymentMethod.CISPAY: CisPayPayment,
    PaymentMethod.DONUT: DonutPayment,
    PaymentMethod.LAVA: LavaPayment,
}


def resolve_payment_method(method: str | None) -> PaymentMethod | None:
    """``'yookassa_sbp'`` → ``PaymentMethod.YOOKASSA``; неизвестное → ``None``."""
    if not method:
        return None
    try:
        return PaymentMethod(method)
    except ValueError:
        pass
    if '_' in method:
        try:
            return PaymentMethod(method.rsplit('_', 1)[0])
        except ValueError:
            return None
    return None


async def link_guest_payment_to_user(
    db: AsyncSession,
    *,
    payment_method: str | None,
    purchase_token: str,
    user_id: int,
) -> bool:
    """Проставить ``user_id`` в строке платежа этой гостевой покупки.

    Без коммита — вызывающий фиксирует это той же транзакцией, что и выдачу.
    Возвращает ``True``, если строка нашлась и обновилась.

    Не трогаем платежи, у которых пользователь уже проставлен: повторный вебхук
    или ретрай выдачи не должны переписывать чужую привязку.
    """
    method = resolve_payment_method(payment_method)
    if method is None:
        return False

    model = _PAYMENT_MODELS.get(method)
    if model is None:
        # Методы без своей таблицы платежей (звёзды, баланс) — это норма.
        logger.debug('Для метода нет таблицы платежей, привязка пропущена', payment_method=payment_method)
        return False

    result = await db.execute(
        update(model)
        .where(
            model.metadata_json['purchase_token'].as_string() == purchase_token,
            model.user_id.is_(None),
        )
        .values(user_id=user_id)
    )
    linked = bool(result.rowcount)
    if linked:
        logger.info(
            'Платёж с лендинга привязан к покупателю',
            payment_method=method.value,
            user_id=user_id,
            purchase_token_prefix=purchase_token[:5],
        )
    return linked
