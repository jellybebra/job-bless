"""Recognized HH redirects that require the account owner's attention."""

from urllib.parse import urlsplit


class HHInterventionRequired(RuntimeError):
    pass


def intervention_message(url: str) -> str:
    address = urlsplit(url.lower())
    host = address.hostname or ""
    if not any(host == domain or host.endswith("." + domain) for domain in ("hh.ru", "hh.kz", "hh.uz", "rabota.by")):
        return ""
    if "captcha" in address.path:
        return "HH просит пройти проверку. Откройте браузер HH в карточке аккаунта и решите капчу."
    if "/account/login" in address.path or "/account/signup" in address.path:
        return "HH требует входа. Откройте браузер HH в карточке аккаунта и войдите снова."
    if any(segment in ("403", "forbidden") for segment in address.path.split("/")):
        return "HH ограничил доступ. Откройте браузер HH и проверьте сообщение сайта."
    return ""
