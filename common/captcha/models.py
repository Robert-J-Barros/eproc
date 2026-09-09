from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class CaptchaType(Enum):
    RECAPTCHA = "recaptcha"
    TURNSTILE = "turnstile"
    HCAPTCHA = "hcaptcha"


@dataclass
class CaptchaDetection:
    tipo: CaptchaType
    sitekey: Optional[str] = None
    frame_url: Optional[str] = None
    action: Optional[str] = None
    page_url: Optional[str] = None


@dataclass
class SolverResult:
    solved: bool
    provider: str
    captcha_type: Optional[str] = None
    elapsed_time: Optional[float] = None
    token: Optional[str] = None
    message: Optional[str] = None
