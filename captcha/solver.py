from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Any

from .models import CaptchaDetection, SolverResult


class CaptchaSolverError(Exception):
    """Erro genérico do solver."""


class CaptchaTimeout(CaptchaSolverError):
    """Tempo limite excedido."""


class CaptchaUnsupported(CaptchaSolverError):
    """Tipo de CAPTCHA não suportado."""


class CaptchaProviderError(CaptchaSolverError):
    """Erro específico do provider."""


class CaptchaProvider(ABC):

    @property
    @abstractmethod
    def name(self) -> str:
        pass

    @abstractmethod
    def solve(
        self,
        context: Any,
        captcha: CaptchaDetection,
    ) -> SolverResult:
        pass


class CaptchaSolver:

    def __init__(self, provider: CaptchaProvider):
        self.provider = provider

    def solve(
        self,
        context: Any,
        captcha: CaptchaDetection,
    ) -> SolverResult:

        started_at = time.monotonic()

        try:
            result = self.provider.solve(
                context,
                captcha,
            )

            result.elapsed_time = time.monotonic() - started_at
            result.provider = self.provider.name
            result.captcha_type = captcha.tipo.value

            if not result.solved:
                raise CaptchaSolverError(
                    result.message
                    or "O provider não conseguiu concluir a operação."
                )

            return result

        except CaptchaSolverError:
            raise

        except Exception as exc:
            raise CaptchaProviderError(
                f"Erro no provider '{self.provider.name}': {exc}"
            ) from exc
