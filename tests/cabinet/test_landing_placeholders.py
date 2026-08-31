"""Плейсхолдеры в текстах лендинга подставляются живыми значениями."""

from types import SimpleNamespace

import pytest

from app.cabinet.utils.landing_placeholders import (
    apply_landing_placeholders,
    lowest_monthly_price_kopeks,
)
from app.config import settings


@pytest.fixture
def trial_days_7(monkeypatch):
    monkeypatch.setattr(settings, 'TRIAL_DURATION_DAYS', 7, raising=False)


def test_trial_period_uses_current_setting(trial_days_7):
    text = apply_landing_placeholders('{trial_period} бесплатно, без карты.')
    assert text == '7 дней бесплатно, без карты.'


def test_trial_days_is_a_bare_number(trial_days_7):
    assert apply_landing_placeholders('Триал: {trial_days}') == 'Триал: 7'


@pytest.mark.parametrize(('days', 'expected'), [(1, '1 день'), (3, '3 дня'), (5, '5 дней')])
def test_russian_plural(monkeypatch, days, expected):
    monkeypatch.setattr(settings, 'TRIAL_DURATION_DAYS', days, raising=False)
    assert apply_landing_placeholders('{trial_period}') == expected


def test_english_period(trial_days_7):
    assert apply_landing_placeholders('{trial_period}', lang='en') == '7 days'


def test_price_from_is_the_cheapest_month():
    tariffs = [
        SimpleNamespace(
            periods=[
                SimpleNamespace(days=30, price_kopeks=18900),
                SimpleNamespace(days=360, price_kopeks=135900),
            ]
        ),
        SimpleNamespace(periods=[SimpleNamespace(days=30, price_kopeks=44900)]),
    ]
    lowest = lowest_monthly_price_kopeks(tariffs)
    assert lowest == 11325
    assert apply_landing_placeholders('от {price_from} в месяц', lowest_monthly_kopeks=lowest) == (
        'от 113 ₽ в месяц'
    )


def test_sub_month_periods_are_ignored():
    tariffs = [SimpleNamespace(periods=[SimpleNamespace(days=1, price_kopeks=1000)])]
    assert lowest_monthly_price_kopeks(tariffs) is None


def test_unknown_braces_are_left_alone(trial_days_7):
    assert apply_landing_placeholders('.card { color: red }') == '.card { color: red }'


def test_empty_text_passes_through():
    assert apply_landing_placeholders(None) is None
    assert apply_landing_placeholders('') == ''
