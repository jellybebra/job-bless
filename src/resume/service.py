"""Importing and managing resumes used as the scoring baseline."""

import logging
from dataclasses import replace
from typing import List, Optional

from src.browser.connector import BrowserConnector
from src.db.models import Resume
from src.resume.parser import HHResumeParser, extract_resume_id, is_resume_url
from src.resume.sections import parse_sections

logger = logging.getLogger(__name__)


class ResumeService:
    def __init__(self, repository, settings, limiter=None):
        self.repository = repository
        self.settings = settings
        self.limiter = limiter
        self.parser = HHResumeParser()

    async def import_from_url(self, resume_url: str, activate: bool = True) -> Resume:
        """Open the resume in the logged-in browser, parse it and store it."""
        url = resume_url.strip()
        if not is_resume_url(url):
            raise ValueError("Ожидается ссылка вида https://hh.ru/resume/<id>")

        browser_config = replace(self.settings.browser_config(), close_stale_tabs=False)
        connector = BrowserConnector(browser_config, limiter=self.limiter)
        async with connector.connect() as page:
            resume = await self.parser.parse(page, url, timeout_ms=browser_config.timeout_ms)

        resume_id = await self._save_parsed(resume)
        resume.id = resume_id
        if activate:
            await self.repository.set_active_resume(resume_id)
            resume.is_active = True

        logger.info("resume imported: id=%s title=%r", resume_id, resume.title)
        return resume

    async def _save_parsed(self, resume: Resume) -> int:
        # Older manual imports may have tracking parameters or a regional HH URL.
        for saved in await self.repository.list_resumes():
            if (saved.external_id or extract_resume_id(saved.source_url)) == resume.external_id:
                resume.source_url = saved.source_url
                break
        return await self.repository.upsert_resume(resume, preserve_user_fields=True)

    async def import_account(self, *, checkpoint=None, log=None, progress=None) -> dict:
        """Import every account resume; retain successes if another card is unavailable."""
        say = log or logger.info
        config = replace(self.settings.browser_config(), close_stale_tabs=False)
        report = {"found": 0, "imported": 0, "failed": 0, "resume_ids": [], "errors": []}
        async with BrowserConnector(config, limiter=self.limiter).connect() as page:
            urls = await self.parser.discover(page, config.timeout_ms, checkpoint=checkpoint)
            report["found"] = len(urls)
            say(f"найдено резюме в аккаунте: {len(urls)}")
            for index, url in enumerate(urls, 1):
                if checkpoint:
                    await checkpoint()
                try:
                    resume = await self.parser.parse(page, url, timeout_ms=config.timeout_ms)
                    if checkpoint:
                        await checkpoint()
                    resume_id = await self._save_parsed(resume)
                    await self.repository.auto_select_resume(resume_id)
                    report["resume_ids"].append(resume_id)
                    report["imported"] += 1
                    say(f"сохранено резюме {index}/{len(urls)}: {resume.title}")
                except PermissionError:
                    raise
                except Exception as error:
                    report["failed"] += 1
                    report["errors"].append({"url": url, "error": str(error)})
                    say(f"не удалось прочитать резюме {index}/{len(urls)}: {error}")
                if progress:
                    progress(index, len(urls))
        return report

    async def refresh(self, resume_id: int) -> Resume:
        saved = await self.repository.get_resume(resume_id)
        if not saved:
            raise ValueError("Резюме не найдено")
        await self.import_from_url(saved.source_url, activate=False)
        return await self.repository.get_resume(resume_id)

    async def backfill_sections(self) -> int:
        """Re-read stored resume text for entries parsed by an older version.

        The page text was always captured in full, so skills, education and the
        rest can be recovered without going back to hh.ru.
        """
        updated = 0
        for resume in await self.repository.list_resumes():
            if not resume.raw_text or (resume.skills and resume.summary):
                continue

            sections = parse_sections(resume.raw_text)
            if not (sections.skills or sections.summary or sections.education or sections.certificates):
                continue

            resume.skills = resume.skills or sections.skills
            resume.summary = resume.summary or sections.summary
            resume.education_text = resume.education_text or sections.education
            resume.certificates = resume.certificates or sections.certificates
            if sections.experience and len(sections.experience) > len(resume.experience_text):
                resume.experience_text = sections.experience

            await self.repository.upsert_resume(resume)
            updated += 1
            logger.info(
                "resume %s re-parsed from stored text: skills=%d certificates=%d",
                resume.id, len(resume.skills), len(resume.certificates),
            )
        return updated

    async def list_resumes(self) -> List[Resume]:
        return await self.repository.list_resumes()

    async def get_active(self) -> Optional[Resume]:
        return await self.repository.get_active_resume()

    async def activate(self, resume_id: int) -> None:
        await self.repository.set_active_resume(resume_id)

    async def delete(self, resume_id: int) -> None:
        await self.repository.delete_resume(resume_id)
