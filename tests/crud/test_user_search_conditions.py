"""Unit tests for the admin user-search condition builder.

Regression guard: a numeric search term that overflows the telegram_id BigInteger
column used to be compared directly, crashing the query (PostgreSQL: value out of
range for type bigint) and spamming the logs. The builder must fall back to
text-only matching for out-of-range numbers.

Считать условия штуками больше нельзя: к трём текстовым колонкам добавился ещё и
телефон, и любое новое поле снова ломало бы такие проверки. Смотрим на то, какие
колонки реально попали в условия.
"""

from __future__ import annotations

from app.database.crud.user import _BIGINT_MAX, _user_search_conditions, phone_search_pattern

TEXT_COLUMNS = 3  # first_name, last_name, username


def _sql(condition) -> str:
    return str(condition.compile(compile_kwargs={'literal_binds': True}))


def _matches(conditions, column: str) -> list[str]:
    return [_sql(c) for c in conditions if column in _sql(c)]


def test_in_range_number_matches_telegram_id() -> None:
    conditions = _user_search_conditions('12345')
    telegram = _matches(conditions, 'telegram_id')
    assert len(telegram) == 1
    assert '12345' in telegram[0]


def test_bigint_max_boundary_still_matches_telegram_id() -> None:
    conditions = _user_search_conditions(str(_BIGINT_MAX))
    assert len(_matches(conditions, 'telegram_id')) == 1


def test_number_over_bigint_max_falls_back_to_text_only() -> None:
    # One past the BIGINT ceiling — would overflow the column and crash the query.
    conditions = _user_search_conditions(str(_BIGINT_MAX + 1))
    assert _matches(conditions, 'telegram_id') == []


def test_very_long_number_falls_back_to_text_only() -> None:
    conditions = _user_search_conditions('9' * 30)
    assert _matches(conditions, 'telegram_id') == []


def test_text_search_never_touches_telegram_id() -> None:
    conditions = _user_search_conditions('john_doe')
    assert len(conditions) == TEXT_COLUMNS
    assert _matches(conditions, 'telegram_id') == []
    assert _matches(conditions, 'users.phone') == []


# ── поиск по номеру телефона ─────────────────────────────────────
def test_pasted_number_is_searched_by_phone() -> None:
    """Номер, вставленный в общее поле поиска, обязан находиться.

    Раньше он молча давал пустой список: искали только по имени, юзернейму и
    telegram_id.
    """
    conditions = _user_search_conditions('+7 928 004-88-81')
    phone = _matches(conditions, 'users.phone')
    assert len(phone) == 1
    assert '9280048881' in phone[0]


def test_short_numbers_do_not_search_by_phone() -> None:
    """«123» иначе вытаскивало бы половину базы."""
    assert _matches(_user_search_conditions('123'), 'users.phone') == []


def test_phone_pattern_ignores_country_code_and_formatting() -> None:
    """Все написания одного номера дают один и тот же шаблон."""
    patterns = {
        phone_search_pattern(raw)
        for raw in ('+79280048881', '89280048881', '9280048881', '+7 (928) 004-88-81')
    }
    assert patterns == {'%9280048881%'}


def test_phone_pattern_ignores_empty_input() -> None:
    assert phone_search_pattern('') is None
    assert phone_search_pattern('не номер') is None
