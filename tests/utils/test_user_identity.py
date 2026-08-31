"""Пользователь со входом по звонку узнаваем везде, где показывают идентификатор.

Прод-репорт: в админ-уведомлении о триале стояло «User#46». У такого аккаунта
заполнен только `phone`, а идентификатор повсюду собирался как
`telegram_id or email or f'#{id}'`.
"""

from __future__ import annotations

from app.database.models import User
from app.utils.user_identity import user_identifier


def test_phone_used_when_nothing_else_known():
    user = User(id=46, telegram_id=None, phone='+79280048881', email=None)
    assert user_identifier(user) == '+79280048881'


def test_telegram_wins_over_phone_and_email():
    user = User(id=1, telegram_id=777, phone='+79280048881', email='a@b.c')
    assert user_identifier(user) == '777'
    assert user_identifier(user, telegram_prefix='ID') == 'ID777'


def test_phone_wins_over_email():
    user = User(id=2, telegram_id=None, phone='+79280048881', email='a@b.c')
    assert user_identifier(user) == '+79280048881'


def test_email_still_shown_when_no_phone():
    user = User(id=3, telegram_id=None, phone=None, email='buyer@example.com')
    assert user_identifier(user) == 'buyer@example.com'


def test_internal_id_is_the_last_resort():
    user = User(id=99, telegram_id=None, phone=None, email=None)
    assert user_identifier(user) == '#99'
    assert user_identifier(user, fallback_prefix='user#') == 'user#99'


def test_prefix_applies_only_to_telegram_id():
    phone_user = User(id=4, telegram_id=None, phone='+79280048881', email=None)
    assert user_identifier(phone_user, telegram_prefix='ID') == '+79280048881'


def test_missing_user_does_not_crash():
    assert user_identifier(None) == '#?'
