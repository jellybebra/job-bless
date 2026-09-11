"""One downloadable CSV containing the saved vacancy cards and their scores."""

import csv
import io
import json


COLUMNS = (
    ("external_id", "ID hh.ru"), ("title", "Вакансия"),
    ("company_name", "Компания"), ("canonical_url", "Ссылка"),
    ("salary_text", "Зарплата"), ("city", "Город"),
    ("work_format", "Формат работы"), ("schedule", "График"),
    ("experience", "Опыт"), ("employment_type", "Занятость"),
    ("snippet", "Описание карточки"), ("skills", "Навыки"),
    ("full_description", "Полное описание вакансии"),
    ("published_at", "Дата публикации"), ("archived", "В архиве"),
    ("response_letter_required", "Обязательное сопроводительное письмо"),
    ("has_test", "Тест или анкета"),
    ("details_fetched_at", "Подробности проверены"), ("details_error", "Ошибка загрузки подробностей"),
    ("raw_text", "Текст карточки"), ("score", "Оценка"),
    ("verdict", "Обоснование оценки"), ("matched_skills", "Совпавшие навыки"),
    ("missing_skills", "Недостающие навыки"), ("application_status", "Статус отклика"),
    ("cover_letter", "Сопроводительное письмо"),
    ("last_discovered_at", "Последний сбор"), ("applied_at", "Дата отклика"),
)


def _cell(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Да" if value else "Нет"
    if isinstance(value, list):
        value = ", ".join(str(item) for item in value)
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        # Scraped text must remain text when opened in a spreadsheet.
        return "'" + value
    return value


def vacancy_csv(rows):
    """UTF-8 BOM and semicolons keep Cyrillic readable in Russian Excel."""
    yield b"\xef\xbb\xbf"
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, delimiter=";")

    def line(values):
        buffer.seek(0)
        buffer.truncate(0)
        writer.writerow(values)
        return buffer.getvalue().encode("utf-8")

    yield line([label for _, label in COLUMNS])
    for row in rows:
        raw = row.get("raw_json") or {}
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (ValueError, TypeError):
                raw = {}
        details = raw if isinstance(raw, dict) else {}
        values = {**details, **row}
        if row.get("key_skills") is not None:
            values["skills"] = row["key_skills"]
        yield line([_cell(values.get(key)) for key, _ in COLUMNS])
