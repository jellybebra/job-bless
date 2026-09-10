import logging
from playwright.async_api import Page
from src.browser.intervention import HHInterventionRequired, intervention_message

logger = logging.getLogger(__name__)


class HHPageGuard:
    """
    Checks for captcha, access denied, or login overlays on HH.ru pages.
    """

    async def check_page_state(self, page: Page, is_navigation_step: bool = False) -> None:
        message = intervention_message(page.url)
        if message:
            raise HHInterventionRequired(message)
