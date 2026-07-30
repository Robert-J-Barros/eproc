from __future__ import annotations

from typing import Any, Optional

from .detector import CaptchaDetector
from .models import CaptchaDetection, CaptchaType, SolverResult
from .solver import (
    CaptchaProvider,
    CaptchaProviderError,
    CaptchaSolver,
    CaptchaSolverError,
    CaptchaTimeout,
    CaptchaUnsupported,
)
from .providers.nopecha import NopeCHAClient, NopeCHAProvider

__all__ = [
    "CaptchaDetector",
    "CaptchaDetection",
    "CaptchaType",
    "SolverResult",
    "CaptchaProvider",
    "CaptchaSolver",
    "CaptchaSolverError",
    "CaptchaTimeout",
    "CaptchaUnsupported",
    "CaptchaProviderError",
    "NopeCHAClient",
    "NopeCHAProvider",
    "resolver_captcha",
]


def resolver_captcha(
    page: Any,
    api_key: Optional[str] = None,
    wait_timeout: float = 5.0,
    overall_timeout: float = 60.0,
) -> Optional[SolverResult]:
    """
    Ponto de entrada único do módulo: detecta e resolve o captcha na
    `page` (Playwright) usando a NopeCHA, com nenhuma configuração além
    da chave de API.

    Retorna None se nenhum captcha for encontrado dentro de
    `wait_timeout` segundos -- ou seja, é seguro chamar isso "só por
    garantia" antes de um submit, mesmo quando não há captcha na tela.

    Exemplo:
        from captcha import resolver_captcha

        resultado = resolver_captcha(page, api_key="minha_chave")
        if resultado:
            print(f"Captcha resolvido em {resultado.elapsed_time:.1f}s")
    """
    detector = CaptchaDetector()
    captcha = detector.wait(page, timeout=wait_timeout)

    if captcha is None:
        return None

    provider = NopeCHAProvider(client=NopeCHAClient(api_key=api_key), overall_timeout=overall_timeout)
    solver = CaptchaSolver(provider)
    return solver.solve(page, captcha)
