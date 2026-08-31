"""Как показать пользователя одной строкой.

У аккаунта, созданного входом по звонку, нет ни ``telegram_id``, ни
``username``, ни имени, ни почты — только номер (см.
:func:`app.database.crud.user.create_user_by_phone`). Пока идентификатор
собирался по месту как ``telegram_id or email or f'#{id}'``, такой человек
показывался во всей админке и во всех уведомлениях как ``#46``: опознать его
было не по чему.

Держим правило в одном месте, чтобы следующий способ входа добавлялся тоже
здесь, а не в сорока f-строках.
"""

from __future__ import annotations

from typing import Any


__all__ = ['user_identifier']


def user_identifier(
    user: Any,
    *,
    telegram_prefix: str = '',
    fallback_prefix: str = '#',
) -> str:
    """Идентификатор пользователя для показа админу и для логов.

    Порядок — от самого узнаваемого к запасному: Telegram ID, номер телефона,
    почта, и только если не известно ничего — внутренний ``id``.

    :param telegram_prefix: приписка перед Telegram ID (``'ID'`` там, где в
        тексте исторически было ``ID123456``). К телефону и почте не
        применяется: они и так самоочевидны.
    :param fallback_prefix: приписка перед внутренним ``id``.
    """
    if user is None:
        return f'{fallback_prefix}?'

    telegram_id = getattr(user, 'telegram_id', None)
    if telegram_id:
        return f'{telegram_prefix}{telegram_id}'

    phone = getattr(user, 'phone', None)
    if phone:
        return str(phone)

    email = getattr(user, 'email', None)
    if email:
        return str(email)

    return f'{fallback_prefix}{getattr(user, "id", "?")}'
