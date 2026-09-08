"""Read the signed-in applicant from hh.ru's rendered account menu."""

import re
from urllib.parse import urlsplit


# Cookie filtering must not touch other sites in the user's browser context.
HH_COOKIE_DOMAIN = re.compile(r"(^|\.)(hh\.(ru|kz|uz)|rabota\.by)$", re.I)
LOGIN_SELECTORS = (
    '[data-qa="profileAndResumes-button"]',
    '[data-qa="vacancyResponses-button"]',
    '[data-qa="applicantProfileDesktopDrop-button"]',
    '[data-qa="applicantProfileMobileDrop-button"]',
    '[data-qa="mainmenu_applicantProfile"]',
    '[data-qa="mainmenu_myResumes"]',
    '[data-qa="mainmenu_negotiations"]',
)
NAME_SELECTORS = (
    '[data-qa="mainmenu_applicantName"]',
    '[data-qa="mainmenu_applicantProfileName"]',
    '[data-qa="mainmenu_applicantProfile"] [data-qa="name"]',
    '[data-qa="mainmenu_applicantProfile"]',
)
MENU_LABELS = {
    "профиль", "мой профиль", "ваш профиль", "личный кабинет", "соискатель",
    "аккаунт", "мой аккаунт", "меню", "аватар", "резюме", "мои резюме",
    "создать резюме", "настройки", "войти", "выйти", "выход",
}
MENU_WORDS = {"профиль", "резюме", "отклики", "настройки", "войти", "выйти", "кабинет", "меню", "аккаунт"}

# Read only account identity from the server-rendered state. The current hh.ru
# header uses an avatar with initials; it no longer contains the full name.
ACCOUNT_STATE_SCRIPT = """() => {
    const template = document.getElementById('HH-Lux-InitialState');
    if (!template) return null;
    try {
        const state = JSON.parse(template.innerHTML);
        const fields = state.applicantProfile?.fields || {};
        const name = ['firstName', 'lastName'].map(key => {
            const value = fields[key];
            return Array.isArray(value) && typeof value[0]?.string === 'string' ? value[0].string : '';
        }).filter(Boolean).join(' ');
        return {user_type: state.userType, name};
    } catch { return null; }
}"""


def is_hh_url(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme in ("http", "https") and bool(HH_COOKIE_DOMAIN.search(parts.hostname or ""))


async def read_hh_account(page) -> dict:
    """Inspect without navigating: the user may still be typing a login code.

    Names are optional. Never substitute a resume title or a menu label when
    hh.ru does not expose the account name in its header.
    """
    account = {"logged_in": False, "account_name": ""}
    if not is_hh_url(page.url):
        return account
    try:
        state = await page.evaluate(ACCOUNT_STATE_SCRIPT)
        if isinstance(state, dict) and state.get("user_type"):
            # anonymousUserType can also be 'applicant', but userType is the
            # authenticated role. Do not mistake the former for a session.
            if state["user_type"] == "applicant":
                account["logged_in"] = True
                name = state.get("name")
                account["account_name"] = " ".join(name.split())[:100] if isinstance(name, str) else ""
            return account
    except Exception:
        pass  # Older headers or a document currently being replaced.
    try:
        for selector in LOGIN_SELECTORS:
            element = await page.query_selector(selector)
            if element and await element.is_visible():
                # Legacy selectors can be on the span inside a login link.
                href = await element.get_attribute("href") or ""
                if "/account/login" not in href:
                    account["logged_in"] = True
                    break
        if not account["logged_in"]:
            return account
        for selector in NAME_SELECTORS:
            element = await page.query_selector(selector)
            if not element:
                continue
            # Some header variants expose the name only on the avatar button.
            candidates = [await element.inner_text(), await element.get_attribute("aria-label"),
                          await element.get_attribute("title")]
            for candidate in candidates:
                name = " ".join((candidate or "").split())
                if (name and len(name) <= 100 and name.casefold() not in MENU_LABELS
                        and not MENU_WORDS.intersection(name.casefold().split())
                        and re.fullmatch(r"[^\W\d_]+(?:[\s’'\-][^\W\d_]+){0,5}", name)):
                    account["account_name"] = name
                    return account
    except Exception:  # Navigation can replace the document between reads.
        pass
    return account
