"""Живые значения в текстах лендинга.

Тексты витрины редактируются в админке и лежат в базе, а числа в них —
длительность триала, нижняя цена — живут в настройках бота и тарифах. Стоит
поменять триал с 3 дней на 7 — и заголовок «3 дня бесплатно» врёт, пока кто-то
не вспомнит про него. Поэтому в тексте пишется плейсхолдер, а подстановка
происходит при отдаче конфига.

Поддерживаются:
    {trial_days}    → «7»
    {trial_period}  → «7 дней» (со склонением для русского)
    {price_from}    → «113 ₽» — самая низкая цена за месяц среди тарифов витрины

Неизвестные фигурные скобки не трогаем: в тексте может быть CSS или просто
скобка, и падать из-за неё лендинг не должен.
"""

from __future__ import annotations

from app.config import settings
from app.utils.pricing_utils import _pluralize_days_ru

DAYS_IN_MONTH = 30


def _trial_period(days: int, lang: str) -> str:
    if lang == 'ru':
        return f'{days} {_pluralize_days_ru(days)}'
    return f'{days} days' if days != 1 else '1 day'


def monthly_rate_kopeks(price_kopeks: int, days: int) -> int | None:
    """Цена за месяц для периода длиной `days` дней."""
    if days <= 0:
        return None
    return round(price_kopeks * DAYS_IN_MONTH / days)


def lowest_monthly_price_kopeks(tariffs) -> int | None:
    """Самая низкая цена за месяц по всем тарифам и периодам витрины.

    Периоды короче месяца пропускаем: у посуточного тарифа «цена за месяц»
    получается умножением и вводит в заблуждение.
    """
    rates = [
        rate
        for tariff in tariffs
        for period in tariff.periods
        if period.days >= DAYS_IN_MONTH
        for rate in (monthly_rate_kopeks(period.price_kopeks, period.days),)
        if rate is not None
    ]
    return min(rates) if rates else None


def _format_price(kopeks: int) -> str:
    rubles = round(kopeks / 100)
    return f'{rubles} ₽'


def apply_landing_placeholders(
    text: str | None,
    *,
    lang: str = 'ru',
    lowest_monthly_kopeks: int | None = None,
) -> str | None:
    """Подставляет живые значения в один текст лендинга."""
    if not text:
        return text

    trial_days = settings.TRIAL_DURATION_DAYS
    replacements = {
        '{trial_days}': str(trial_days),
        '{trial_period}': _trial_period(trial_days, lang),
    }
    if lowest_monthly_kopeks is not None:
        replacements['{price_from}'] = _format_price(lowest_monthly_kopeks)

    for placeholder, value in replacements.items():
        if placeholder in text:
            text = text.replace(placeholder, value)
    return text
