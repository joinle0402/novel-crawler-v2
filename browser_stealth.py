"""Playwright stealth — giảm dấu hiệu automation (áp dụng trước page.goto)."""

from __future__ import annotations

import importlib
from typing import Any

from playwright.sync_api import BrowserContext, Page

_REAL_BROWSER_CHANNELS = frozenset({"chrome", "msedge", "msedge-beta", "msedge-dev"})


def _stealth_languages(locale: str) -> tuple[str, ...]:
    base = locale.split("-")[0]
    if base and base != locale:
        return (locale, base)
    return (locale,)


def _uses_real_browser(channel: str | None) -> bool:
    return bool(channel and channel.lower() in _REAL_BROWSER_CHANNELS)


def _skip_chrome_object_evasions(*, headless: bool, channel: str | None) -> bool:
    """Headed hoặc Chrome/Edge thật — không mock window.chrome (dễ xung đột)."""
    return _uses_real_browser(channel) or not headless


def build_stealth_launch_kwargs(base: dict[str, Any], *, channel: str | None) -> dict[str, Any]:
    kwargs = dict(base)
    if channel:
        kwargs["channel"] = channel
    return kwargs


def build_stealth_context_kwargs(
    base: dict[str, Any],
    *,
    locale: str,
    timezone: str,
) -> dict[str, Any]:
    kwargs = dict(base)
    if locale:
        kwargs.setdefault("locale", locale)
    if timezone:
        kwargs.setdefault("timezone_id", timezone)
    return kwargs


def _stealth_v2_kwargs(languages: tuple[str, ...], *, headless: bool, channel: str | None) -> dict[str, Any]:
    skip_chrome = _skip_chrome_object_evasions(headless=headless, channel=channel)
    return {
        "navigator_languages_override": languages,
        "chrome_load_times": not skip_chrome,
        "chrome_runtime": False,
        "chrome_app": not skip_chrome,
        "chrome_csi": not skip_chrome,
    }


def _apply_stealth_v2(
    context: BrowserContext,
    *,
    languages: tuple[str, ...],
    headless: bool,
    channel: str | None,
) -> None:
    ps = importlib.import_module("playwright_stealth")
    stealth = ps.Stealth(**_stealth_v2_kwargs(languages, headless=headless, channel=channel))
    stealth.apply_stealth_sync(context)


def _apply_stealth_v1(
    context: BrowserContext,
    *,
    languages: tuple[str, ...],
    headless: bool,
    channel: str | None,
) -> None:
    ps = importlib.import_module("playwright_stealth")
    skip_chrome = _skip_chrome_object_evasions(headless=headless, channel=channel)
    config = ps.StealthConfig(
        languages=languages,
        chrome_load_times=not skip_chrome,
        chrome_runtime=False,
        chrome_app=not skip_chrome,
        chrome_csi=not skip_chrome,
    )
    # v1 gọi add_init_script từng đoạn → utils/opts mất scope trên Playwright mới.
    # Gộp một khối để biến dùng chung giữa các evasion.
    combined = "\n\n".join(config.enabled_scripts)
    context.add_init_script(combined)


def apply_stealth(
    context: BrowserContext,
    page: Page,
    *,
    enabled: bool,
    locale: str,
    headless: bool,
    channel: str | None,
) -> None:
    """Inject evasion scripts — gọi sau new_page(), trước goto()."""
    del page  # v2/v1 đều áp dụng trên context; giữ tham số để API ổn định
    if not enabled:
        return

    languages = _stealth_languages(locale or "vi-VN")
    ps = importlib.import_module("playwright_stealth")

    if hasattr(ps, "Stealth"):
        _apply_stealth_v2(context, languages=languages, headless=headless, channel=channel)
        return

    _apply_stealth_v1(context, languages=languages, headless=headless, channel=channel)
