"""Parsing of an hh.ru resume page into the `Resume` entity.

Structured fields are best-effort: hh.ru markup changes often, so the whole
page text is always kept in `raw_text` and used as the scoring fallback.
"""

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import List, Optional
from urllib.parse import urljoin, urlsplit, urlunsplit
from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from src.db.models import Resume
from src.resume.sections import parse_sections

logger = logging.getLogger(__name__)

RESUME_URL_RE = re.compile(r"^https?://((?:[a-z0-9-]+\.)*)(hh\.ru|hh\.kz|hh\.uz|rabota\.by)/resume/([0-9a-zA-Z]+)(?:[/?#]|$)", re.I)
MY_RESUMES_URL = "https://hh.ru/applicant/resumes"

TITLE_SELECTORS = ['[data-qa="resume-block-title-position"]', '[data-qa="resume-block-position"]']
NAME_SELECTORS = ['[data-qa="resume-personal-name"]']
EXPERIENCE_TOTAL_SELECTORS = [
    '[data-qa="resume-block-experience"] .bloko-text_strong',
    '[data-qa="resume-block-experience"] h2 + span',
    '[data-qa="resume-block-experience"] .resume-block__title-text_sub',
]
SUMMARY_SELECTORS = ['[data-qa="resume-block-skills-content"]', '[data-qa="resume-block-about"]']
SKILL_SELECTORS = [
    '[data-qa="skills-table"] [data-qa="bloko-tag__text"]',
    '[data-qa="resume-block-skills"] [data-qa="bloko-tag__text"]',
    '.bloko-tag-list [data-qa="bloko-tag__text"]',
]
EXPERIENCE_ITEM_SELECTORS = ['[data-qa="resume-block-experience"] .resume-block-item-gap']

# Read only resume certificates and verification data, excluding suggested tests
# and the sidebar's offers to obtain certificates from third-party services.
RESUME_DETAILS_SCRIPT = """() => {
    try {
        const template = document.getElementById('HH-Lux-InitialState');
        const resume = JSON.parse(template.innerHTML).applicantResume;
        return {
            certificates: Array.isArray(resume.certificate) ? resume.certificate.map(item => ({
                title: item.title, achievementDate: item.achievementDate
            })) : null,
            skills: Array.isArray(resume.resumeApplicantSkills) ? resume.resumeApplicantSkills.map(skill => ({
                name: skill.name, verified: skill.verified,
                verifications: skill.verifications?.map(item => ({
                    status: item.result?.status, state: item.validity?.state,
                    validUntil: item.validity?.validUntil
                }))
            })) : null
        };
    } catch { return {}; }
}"""


def extract_resume_id(url: str) -> str:
    match = RESUME_URL_RE.match(url.strip())
    return match.group(3) if match else ""


def is_resume_url(url: str) -> bool:
    return bool(RESUME_URL_RE.match(url.strip()))


class HHResumeParser:
    """Opens a resume page in an authenticated browser and extracts its content."""

    async def discover(self, page: Page, timeout_ms: int = 30000, checkpoint=None) -> List[str]:
        """Collect distinct resume links from the applicant's own list, including pages."""
        urls = {}
        visited = set()
        next_url = MY_RESUMES_URL
        while next_url and next_url not in visited:
            if checkpoint:
                await checkpoint()
            visited.add(next_url)
            response = await page.goto(next_url, wait_until="domcontentloaded", timeout=timeout_ms)
            await self._check_access(page, response)
            try:
                await page.wait_for_function("""() =>
                    document.querySelector('a[href*="/resume/"]') ||
                    /нет резюме|пока не создали.*резюме|создайте первое резюме/i.test(document.body.innerText)
                """, timeout=timeout_ms)
            except PlaywrightTimeoutError as error:
                await self._check_access(page)
                raise ValueError("Не удалось прочитать список резюме. Проверьте вход в hh.ru и повторите обновление.") from error
            for href in await page.locator('a[href*="/resume/"]').evaluate_all(
                "elements => elements.map(el => el.href)"
            ):
                if is_resume_url(href):
                    parts = urlsplit(href)
                    resume_id = extract_resume_id(href)
                    urls.setdefault(resume_id, urlunsplit((parts.scheme, parts.netloc, f"/resume/{resume_id}", "", "")))
            next_link = page.locator('a[data-qa="pager-next"]').first
            href = await next_link.get_attribute("href") if await next_link.count() else None
            candidate = urljoin(page.url, href) if href else ""
            parts = urlsplit(candidate)
            hh_host = parts.hostname == "hh.ru" or (parts.hostname or "").endswith(".hh.ru")
            next_url = candidate if hh_host and parts.path == "/applicant/resumes" else ""
        return list(urls.values())

    async def _check_access(self, page: Page, response=None) -> None:
        if "/account/login" in page.url or "/auth/" in page.url:
            raise PermissionError("Войдите в hh.ru на странице «Действия» и повторите обновление.")
        if "/captcha" in page.url or await page.locator('[data-qa="captcha-input"], input[name="captcha"]').count():
            raise PermissionError("hh.ru показал капчу. Откройте браузер HH в панели, пройдите проверку и повторите обновление.")
        if response and response.status >= 400:
            raise ValueError(f"hh.ru не открыл страницу резюме (HTTP {response.status}). Откройте браузер HH в панели и проверьте вход в аккаунт.")

    async def parse(self, page: Page, resume_url: str, timeout_ms: int = 30000) -> Resume:
        logger.info("opening resume page: %s", resume_url)
        response = await page.goto(resume_url, wait_until="domcontentloaded", timeout=timeout_ms)
        await asyncio.sleep(1.5)
        await self._check_access(page, response)

        if "/account/login" in page.url or "/auth/" in page.url:
            raise PermissionError(
                "hh.ru потребовал вход. Войдите в аккаунт через кнопку «Войти в hh.ru» и повторите импорт."
            )

        raw_text = await self._page_text(page)
        if not raw_text.strip():
            raise ValueError("страница резюме пустая — возможно, ссылка неверная или доступ закрыт")
        title = await self._first_text(page, TITLE_SELECTORS)
        if not title:
            raise ValueError("Не удалось прочитать резюме: нет заголовка. Сохранённые данные не изменены.")

        # Section headings survive hh.ru markup changes, CSS selectors do not,
        # so the text split is the primary source and the DOM only refines it.
        sections = parse_sections(raw_text)
        details = await self._details(page)
        certificates = sections.certificates
        if isinstance(details.get("certificates"), list):
            certificates = parse_certificates(details["certificates"])
        if isinstance(details.get("skills"), list):
            verified_skills = parse_verified_skills(details["skills"])
        else:
            verified_skills = await self._verified_skill_tags(page)

        resume = Resume(
            source_url=resume_url,
            external_id=extract_resume_id(resume_url),
            title=title,
            full_name=await self._first_text(page, NAME_SELECTORS),
            summary=sections.summary or await self._first_text(page, SUMMARY_SELECTORS),
            skills=sections.skills or await self._skills(page),
            verified_skills=verified_skills,
            education_text=sections.education,
            certificates=certificates,
            raw_text=raw_text[:60000],
        )
        resume.experience_text = sections.experience or await self._experience_text(page, raw_text)

        logger.info(
            "resume parsed: title=%r skills=%d experience=%d chars education=%s "
            "certificates=%d summary=%d chars text=%d chars",
            resume.title, len(resume.skills), len(resume.experience_text),
            bool(resume.education_text), len(resume.certificates),
            len(resume.summary), len(resume.raw_text),
        )
        if not resume.skills and not resume.summary:
            logger.warning(
                "resume %s parsed without skills or summary — hh.ru markup may have changed",
                resume_url,
            )
        return resume

    async def _details(self, page: Page) -> dict:
        try:
            details = await page.evaluate(RESUME_DETAILS_SCRIPT)
            return details if isinstance(details, dict) else {}
        except Exception:
            return {}

    async def _verified_skill_tags(self, page: Page) -> List[str]:
        # Current HH marks verified resume tags as positive. The same names in
        # skills-methods are merely suggestions to take a test and must be ignored.
        tags = await page.locator(
            '[data-qa="skills-card"] [data-qa^="skill-tag-"][class*="magritte-tag_style-positive"]'
        ).all_text_contents()
        return list(dict.fromkeys(_clean(tag) for tag in tags if tag.strip()))

    async def _first_text(self, page: Page, selectors: List[str]) -> str:
        for selector in selectors:
            try:
                element = await page.query_selector(selector)
                if element:
                    text = (await element.inner_text()).strip()
                    if text:
                        return _clean(text)
            except Exception:
                continue
        return ""

    async def _skills(self, page: Page) -> List[str]:
        skills: List[str] = []
        for selector in SKILL_SELECTORS:
            try:
                elements = await page.query_selector_all(selector)
            except Exception:
                continue
            for element in elements:
                try:
                    text = _clean((await element.inner_text()).strip())
                except Exception:
                    continue
                if text and text not in skills:
                    skills.append(text)
            if skills:
                break
        return skills[:100]

    async def _experience_text(self, page: Page, raw_text: str) -> str:
        header = await self._first_text(page, EXPERIENCE_TOTAL_SELECTORS)
        if header:
            return header

        match = re.search(r"Опыт работы\s*[—-]?\s*([^\n]{0,60})", raw_text)
        if match:
            return _clean(match.group(0))

        # Fall back to the list of positions, which is enough context for scoring.
        try:
            items = await page.query_selector_all(EXPERIENCE_ITEM_SELECTORS[0])
        except Exception:
            return ""
        chunks = []
        for item in items[:10]:
            try:
                chunks.append(_clean((await item.inner_text()).strip()))
            except Exception:
                continue
        return "\n".join(c for c in chunks if c)[:4000]

    async def _page_text(self, page: Page) -> str:
        for selector in ('[data-qa="resume"]', "main", "body"):
            try:
                element = await page.query_selector(selector)
                if element:
                    text = await element.inner_text()
                    if text and text.strip():
                        return _clean(text)
            except Exception:
                continue
        return ""


def _clean(text: str) -> str:
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def parse_certificates(items: list) -> List[str]:
    certificates = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("title"), str):
            continue
        title = _clean(item["title"])
        if not title:
            continue
        date = str(item.get("achievementDate") or "")
        if re.match(r"^(19|20)\d{2}(?:-|$)", date):
            title = f"{title} ({date[:4]})"
        if title not in certificates:
            certificates.append(title)
    return certificates


def parse_verified_skills(items: list, now: Optional[datetime] = None) -> List[str]:
    """Only HH-confirmed skills with an effective successful result qualify."""
    now = now or datetime.now(timezone.utc)
    names = []
    for item in items:
        if not isinstance(item, dict) or item.get("verified") is not True:
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        verifications = item.get("verifications")
        if verifications and not any(_verification_current(result, now) for result in verifications):
            continue
        name = _clean(name)
        if name not in names:
            names.append(name)
    return names


def _verification_current(result: dict, now: datetime) -> bool:
    if not isinstance(result, dict) or result.get("status") != "SUCCESS" or result.get("state") != "EFFECTIVE":
        return False
    if not result.get("validUntil"):
        return True
    try:
        expires = datetime.fromisoformat(result["validUntil"])
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return expires > now
    except (TypeError, ValueError):
        return False
