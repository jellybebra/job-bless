"""Read vacancy details without opening or submitting the response form."""

import asyncio
import re
from dataclasses import replace
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urlsplit

from playwright.async_api import Page

from src.browser.connector import BrowserConnector, HH_URL_RE
from src.browser.intervention import HHInterventionRequired
from src.collector.page_guard import HHPageGuard
from src.db.models import VacancyCard, VacancyDetails


# Read only the current vacancy, never translated UI messages or recommendations.
# In particular, "Задайте вопрос работодателю" is NOT an employer questionnaire.
DETAIL_SNAPSHOT = r"""() => {
    const el = document.getElementById('HH-Lux-InitialState');
    let state = {};
    try { state = JSON.parse(el?.content?.textContent || el?.textContent || '{}'); } catch (_) {}
    const view = state.vacancyView || {};
    const short = state.applicantVacancyResponseStatuses?.[String(view.vacancyId)]?.shortVacancy || {};
    const postings = [];
    function collectJsonLd(value) {
        if (Array.isArray(value)) return value.forEach(collectJsonLd);
        if (!value || typeof value !== 'object') return;
        if ([value['@type']].flat().includes('JobPosting')) postings.push(value);
        if (value['@graph']) collectJsonLd(value['@graph']);
    }
    document.querySelectorAll('script[type="application/ld+json"]').forEach(script => {
        try { collectJsonLd(JSON.parse(script.textContent)); } catch (_) {}
    });
    const description = document.querySelector('[data-qa="vacancy-description"], .vacancy-description');
    return {
        vacancy_id: view.vacancyId,
        description_html: view.description,
        key_skills: view.keySkills?.keySkill,
        published_at: view.publicationDate,
        archived: view.status?.archived,
        response_letter_required: short['@responseLetterRequired'],
        has_test: short.userTestPresent,
        user_test_id: view.userTestId,
        postings,
        dom_description: description?.innerText || '',
        dom_skills: Array.from(document.querySelectorAll('[data-qa="skills-element"]')).map(e => e.innerText),
        dom_archived: !!document.querySelector('[data-qa="vacancy-title-archived-text"], [data-qa="vacancy-archive-description"]'),
        dom_active: !!document.querySelector('[data-qa="vacancy-response-link-top"], [data-qa="vacancy-response-link-bottom"]')
    };
}"""


class _DescriptionText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.hidden += 1
        elif not self.hidden and tag in ('p', 'div', 'br', 'li', 'ul', 'ol', 'h1', 'h2', 'h3', 'tr'):
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.hidden = max(0, self.hidden - 1)
        elif not self.hidden and tag in ('p', 'div', 'li', 'ul', 'ol', 'h1', 'h2', 'h3', 'tr'):
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def description_text(value: str) -> str:
    parser = _DescriptionText()
    parser.feed(value)
    return '\n'.join(line.strip() for line in ''.join(parser.parts).splitlines() if line.strip())


def _boolean(value):
    return value if isinstance(value, bool) else None


def _date(value) -> str:
    if not isinstance(value, str) or not value.strip():
        return ''
    try:
        return datetime.fromisoformat(value.strip().replace('Z', '+00:00')).isoformat()
    except ValueError:
        return ''


def parse_details(data: dict, external_id: str) -> VacancyDetails:
    """Missing flags stay unknown; absence of a badge does not mean false."""
    view_id = data.get('vacancy_id')
    if view_id is not None and str(view_id) != external_id:
        raise ValueError('HH вернул другую вакансию')

    posting = {}
    for candidate in data.get('postings') or []:
        identifier = candidate.get('identifier') or {}
        value = identifier.get('value') if isinstance(identifier, dict) else identifier
        if str(value) == external_id:
            posting = candidate
            break

    html = data.get('description_html') or posting.get('description') or ''
    description = description_text(html) if isinstance(html, str) else ''
    description = description or str(data.get('dom_description') or '').strip()
    skills = data.get('key_skills')
    if not isinstance(skills, list):
        skills = data.get('dom_skills') or None
    if skills is not None:
        skills = list(dict.fromkeys(s.strip() for s in skills if isinstance(s, str) and s.strip()))

    archived = _boolean(data.get('archived'))
    if archived is None:
        if data.get('dom_archived'):
            archived = True
        elif data.get('dom_active') and description:
            archived = False
    has_test = _boolean(data.get('has_test'))
    # A positive test ID proves a test exists; null alone is not a negative answer.
    if has_test is None and isinstance(data.get('user_test_id'), int) and data['user_test_id'] > 0:
        has_test = True
    if not description and archived is None:
        raise ValueError('На странице не найдены данные вакансии')

    return VacancyDetails(
        full_description=description,
        key_skills=skills,
        published_at=_date(data.get('published_at') or posting.get('datePosted')),
        archived=archived,
        response_letter_required=_boolean(data.get('response_letter_required')),
        has_test=has_test,
        fetched_at=datetime.now(timezone.utc).isoformat(),
    )


class VacancyDetailsReader:
    """Reuse one extra tab while the collector keeps its search page open."""

    def __init__(self, browser_config, *, limiter, should_stop):
        self.config = browser_config
        self.limiter = limiter
        self.should_stop = should_stop
        self.page = None
        self.connection = None

    async def __aenter__(self):
        # Opening the tab is lazy: an empty search never needs a detail tab.
        return self

    async def __aexit__(self, *exc):
        if self.connection:
            await self.connection.__aexit__(*exc)

    def _check_stop(self):
        if self.should_stop():
            raise asyncio.CancelledError('stopped while collecting vacancy details')

    async def read(self, card: VacancyCard) -> VacancyDetails:
        self._check_stop()
        if not card.external_id.isdigit() or not HH_URL_RE.match(card.url):
            raise ValueError('Нет прямой ссылки на вакансию HH')
        if self.page is None:
            connector = BrowserConnector(replace(self.config, close_stale_tabs=False), limiter=self.limiter)
            self.connection = connector.connect()
            self.page = await self.connection.__aenter__()
        if self.limiter:
            await self.limiter.acquire(should_stop=self.should_stop)
        self._check_stop()
        response = await self.page.goto(card.url, wait_until='domcontentloaded', timeout=self.config.timeout_ms)
        self._check_stop()
        await HHPageGuard().check_page_state(self.page, is_navigation_step=True)
        if response and response.status in (401, 403, 429):
            raise HHInterventionRequired('HH ограничил доступ к вакансиям. Проверьте браузер HH в карточке аккаунта.')
        if response and response.status >= 400:
            raise ValueError(f'Страница вакансии недоступна (HTTP {response.status})')
        match = re.search(r'/vacancy/(\d+)', urlsplit(self.page.url).path)
        if not match or match.group(1) != card.external_id:
            raise ValueError('HH перенаправил на другую страницу')
        await self.page.wait_for_selector(
            '#HH-Lux-InitialState, [data-qa="vacancy-description"], [data-qa="vacancy-title-archived-text"], script[type="application/ld+json"]',
            state='attached', timeout=self.config.timeout_ms,
        )
        self._check_stop()
        return parse_details(await self.page.evaluate(DETAIL_SNAPSHOT), card.external_id)
