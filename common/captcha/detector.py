from __future__ import annotations

import time
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from .models import CaptchaDetection, CaptchaType


class CaptchaDetector:
    """Detector independente da implementação de solver."""

    def detect(self, context: Any) -> Optional[CaptchaDetection]:
        for detector in (
            self._detect_turnstile,
            self._detect_recaptcha,
            self._detect_hcaptcha,
        ):
            captcha = detector(context)
            if captcha is not None:
                return captcha
        return None

    def wait(
        self,
        context: Any,
        timeout: float = 5,
        interval: float = 0.5,
    ) -> Optional[CaptchaDetection]:
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            captcha = self.detect(context)
            if captcha is not None:
                return captcha
            time.sleep(interval)

        return None

    def _detect_turnstile(self, context: Any) -> Optional[CaptchaDetection]:
        iframe = context.locator("iframe[src*='turnstile']")

        if iframe.count() > 0:
            src = iframe.first.get_attribute("src")
            return CaptchaDetection(
                tipo=CaptchaType.TURNSTILE,
                sitekey=self._extract_sitekey(src),
                frame_url=src,
                page_url=self._get_page_url(context),
            )

        element = context.locator(".cf-turnstile")

        if element.count() > 0:
            return CaptchaDetection(
                tipo=CaptchaType.TURNSTILE,
                sitekey=element.first.get_attribute("data-sitekey"),
                action=element.first.get_attribute("data-action"),
                page_url=self._get_page_url(context),
            )

        return None

    def _detect_recaptcha(self, context: Any) -> Optional[CaptchaDetection]:
        iframe = context.locator("iframe[src*='recaptcha']")

        if iframe.count() > 0:
            src = iframe.first.get_attribute("src")
            return CaptchaDetection(
                tipo=CaptchaType.RECAPTCHA,
                sitekey=self._extract_sitekey(src),
                frame_url=src,
                page_url=self._get_page_url(context),
            )

        element = context.locator(".g-recaptcha")

        if element.count() > 0:
            return CaptchaDetection(
                tipo=CaptchaType.RECAPTCHA,
                sitekey=element.first.get_attribute("data-sitekey"),
                action=element.first.get_attribute("data-action"),
                page_url=self._get_page_url(context),
            )

        return None

    def _detect_hcaptcha(self, context: Any) -> Optional[CaptchaDetection]:
        iframe = context.locator("iframe[src*='hcaptcha']")

        if iframe.count() > 0:
            src = iframe.first.get_attribute("src")
            return CaptchaDetection(
                tipo=CaptchaType.HCAPTCHA,
                sitekey=self._extract_sitekey(src),
                frame_url=src,
                page_url=self._get_page_url(context),
            )

        element = context.locator(".h-captcha")

        if element.count() > 0:
            return CaptchaDetection(
                tipo=CaptchaType.HCAPTCHA,
                sitekey=element.first.get_attribute("data-sitekey"),
                page_url=self._get_page_url(context),
            )

        return None

    @staticmethod
    def _extract_sitekey(src: Optional[str]) -> Optional[str]:
        if not src:
            return None

        try:
            query = parse_qs(urlparse(src).query)

            if "k" in query:
                return query["k"][0]

            if "sitekey" in query:
                return query["sitekey"][0]

        except Exception:
            pass

        return None

    @staticmethod
    def _get_page_url(context: Any) -> Optional[str]:
        return getattr(context, "url", None)


def detectar(context: Any) -> Optional[CaptchaDetection]:
    return CaptchaDetector().detect(context)


def existe_captcha(context: Any) -> bool:
    return detectar(context) is not None


def imprimir_diagnostico(context: Any) -> None:
    captcha = detectar(context)

    if captcha is None:
        print("[Captcha] Nenhum CAPTCHA encontrado.")
        return

    print("=" * 60)
    print("CAPTCHA DETECTADO")
    print("=" * 60)
    print(f"Tipo      : {captcha.tipo.value}")
    print(f"SiteKey   : {captcha.sitekey}")
    print(f"Iframe URL: {captcha.frame_url}")
    print(f"Action    : {captcha.action}")
    print(f"Page URL  : {captcha.page_url}")
    print("=" * 60)
