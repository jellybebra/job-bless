"""Browser transport shared by login and background jobs."""

from src.browser.errors import is_transient_browser_error

async def connect_browser(playwright, config):
    if not config.endpoint:
        raise RuntimeError("Браузер HH не запущен. Откройте job-bless с работающим Docker.")
    try:
        return await playwright.firefox.connect(config.endpoint, timeout=config.timeout_ms)
    except Exception as error:
        if is_transient_browser_error(error):
            raise
        raise RuntimeError("Не удалось подключиться к Camoufox. Проверьте, что Docker и контейнер HH работают.") from error
