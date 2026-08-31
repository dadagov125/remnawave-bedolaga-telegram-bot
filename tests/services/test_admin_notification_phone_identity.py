"""Пользователь, зарегистрированный звонком, узнаваем в админ-уведомлениях.

Прод-репорт: в «АКТИВАЦИЯ ТРИАЛА» на человека с входом по номеру приходило
«Пользователь: User#46 / ID: User#46 / Username: @отсутствует» — опознать его
было не по чему. У такого аккаунта нет ни telegram_id, ни username, ни имени,
ни почты (см. crud.user.create_user_by_phone), а хелперы уведомлений знали
только про telegram_id и email и падали в «User#<id>».
"""

from __future__ import annotations

from app.database.models import User
from app.services.admin_notification_service import AdminNotificationService


def _service() -> AdminNotificationService:
    return AdminNotificationService.__new__(AdminNotificationService)


def _phone_user() -> User:
    return User(
        id=46,
        telegram_id=None,
        auth_type='phone',
        phone='+79280048881',
        username=None,
        first_name=None,
        last_name=None,
        email=None,
    )


def test_display_uses_phone_when_nothing_else_identifies_user():
    assert _service()._get_user_display(_phone_user()) == '+79280048881'


def test_identifier_shows_phone_copyable():
    display = _service()._get_user_identifier_display(_phone_user())
    assert '+79280048881' in display
    assert '<code>' in display, 'номер должен копироваться одним тапом'


def test_identifier_label_says_phone():
    assert _service()._get_user_identifier_label(_phone_user()) == 'Телефон'


def test_telegram_still_wins_over_phone():
    user = User(id=1, telegram_id=777, username='vasya', first_name='Вася', phone='+79280048881')
    service = _service()
    assert service._get_user_display(user) == 'Вася'
    assert service._get_user_identifier_display(user) == '<code>777</code>'
    assert service._get_user_identifier_label(user) == 'Telegram ID'


def test_email_user_unchanged():
    user = User(id=2, telegram_id=None, email='buyer@example.com', phone=None)
    service = _service()
    assert service._get_user_display(user) == 'buyer@example.com'
    assert service._get_user_identifier_display(user) == '📧 buyer@example.com'
    assert service._get_user_identifier_label(user) == 'Email'


def test_falls_back_to_id_when_nothing_known():
    user = User(id=99, telegram_id=None, email=None, phone=None)
    service = _service()
    assert service._get_user_display(user) == 'User#99'
    assert service._get_user_identifier_label(user) == 'ID'
