from __future__ import annotations

import os
import time
from typing import Any, Optional

import requests

from ..models import CaptchaDetection, CaptchaType, SolverResult
from ..solver import CaptchaProvider, CaptchaProviderError, CaptchaTimeout, CaptchaUnsupported

# Mapeamento do nosso CaptchaType interno -> valor esperado pela API da
# NopeCHA no campo "type" do POST /token.
# https://developers.nopecha.com/api/recognition (endpoint /token)
_NOPECHA_TYPE_MAP = {
    CaptchaType.TURNSTILE: "turnstile",
    CaptchaType.RECAPTCHA: "recaptcha2",
    CaptchaType.HCAPTCHA: "hcaptcha",
}


class NopeCHAClient:
    """
    Cliente HTTP para a API de resolução por token da NopeCHA
    (https://api.nopecha.com). Fluxo: POST /token para enfileirar o
    job -> GET /token?id=... em polling até sair um token pronto.

    Uso da API sujeito aos Termos de Serviço da NopeCHA e à
    autorização do ambiente onde for utilizado.
    """

    BASE_URL = "https://api.nopecha.com"

    def __init__(
        self,
        api_key: Optional[str] = None,
        timeout: int = 30,
    ):
        self.api_key = api_key or os.getenv("NOPECHA_API_KEY")
        self.timeout = timeout

        if not self.api_key:
            raise ValueError("NOPECHA_API_KEY não configurada.")

    def _headers(self) -> dict[str, str]:
        return {"Content-Type": "application/json"}

    def solve_token(
        self,
        captcha_type: str,
        sitekey: str,
        url: str,
        poll_interval: float = 2.0,
        overall_timeout: float = 60.0,
    ) -> str:
        """
        Envia o desafio para resolução e faz polling até obter o token.
        Levanta CaptchaProviderError / CaptchaTimeout em caso de falha.
        """
        job_id = self._submit(captcha_type, sitekey, url)
        return self._poll(job_id, poll_interval=poll_interval, overall_timeout=overall_timeout)

    def _submit(self, captcha_type: str, sitekey: str, url: str) -> str:
        payload = {
            "key": self.api_key,
            "type": captcha_type,
            "sitekey": sitekey,
            "url": url,
        }

        response = requests.post(
            f"{self.BASE_URL}/token",
            json=payload,
            headers=self._headers(),
            timeout=self.timeout,
        )

        if response.status_code >= 400:
            raise CaptchaProviderError(
                f"NopeCHA rejeitou a submissão (HTTP {response.status_code}): {response.text[:300]}"
            )

        body = response.json()
        job_id = body.get("data")

        if not job_id:
            raise CaptchaProviderError(f"Resposta inesperada da NopeCHA ao submeter: {body!r}")

        return job_id

    def _poll(self, job_id: str, poll_interval: float, overall_timeout: float) -> str:
        deadline = time.monotonic() + overall_timeout

        while time.monotonic() < deadline:
            response = requests.get(
                f"{self.BASE_URL}/token",
                params={"id": job_id, "key": self.api_key},
                timeout=self.timeout,
            )

            if response.status_code == 400:
                # Job ainda processando -- a API da NopeCHA responde 400
                # com "message" enquanto o token não está pronto.
                time.sleep(poll_interval)
                continue

            if response.status_code >= 400:
                raise CaptchaProviderError(
                    f"NopeCHA retornou erro no polling (HTTP {response.status_code}): "
                    f"{response.text[:300]}"
                )

            body = response.json()
            token = body.get("data")

            if token:
                return token

            time.sleep(poll_interval)

        raise CaptchaTimeout(
            f"Tempo limite de {overall_timeout}s excedido aguardando o token da NopeCHA "
            f"(job_id={job_id})."
        )


class NopeCHAProvider(CaptchaProvider):
    """
    Provider que resolve o desafio via API de token da NopeCHA e injeta
    o resultado na página através do contexto do navegador (Playwright
    Page/Frame). Não depende da extensão do navegador.
    """

    def __init__(
        self,
        client: Optional[NopeCHAClient] = None,
        poll_interval: float = 2.0,
        overall_timeout: float = 60.0,
    ):
        self.client = client or NopeCHAClient()
        self.poll_interval = poll_interval
        self.overall_timeout = overall_timeout

    @property
    def name(self) -> str:
        return "nopecha"

    def solve(
        self,
        context: Any,
        captcha: CaptchaDetection,
    ) -> SolverResult:
        nopecha_type = _NOPECHA_TYPE_MAP.get(captcha.tipo)

        if nopecha_type is None:
            raise CaptchaUnsupported(
                f"Tipo de captcha '{captcha.tipo}' não é suportado pelo NopeCHAProvider."
            )

        if not captcha.sitekey:
            raise CaptchaProviderError(
                "Não foi possível extrair o sitekey do captcha detectado -- "
                "sem sitekey a API da NopeCHA não consegue resolver o desafio."
            )

        token = self.client.solve_token(
            captcha_type=nopecha_type,
            sitekey=captcha.sitekey,
            url=captcha.page_url or "",
            poll_interval=self.poll_interval,
            overall_timeout=self.overall_timeout,
        )

        self._inject_token(context, captcha, token)

        return SolverResult(
            solved=True,
            provider=self.name,
            captcha_type=captcha.tipo.value,
            token=token,
        )

    def _inject_token(self, context: Any, captcha: CaptchaDetection, token: str) -> None:
        """
        Coloca o token no campo hidden que o widget cria e dispara o
        callback declarado em data-callback (quando existir), do jeito
        que o próprio widget faria ao resolver visualmente. Isso evita
        que a gente tenha que conhecer a lógica de cada site -- só
        completamos o que o widget oficial já deixou pronto no DOM.
        """
        if captcha.tipo == CaptchaType.TURNSTILE:
            field_name = "cf-turnstile-response"
        elif captcha.tipo == CaptchaType.RECAPTCHA:
            field_name = "g-recaptcha-response"
        elif captcha.tipo == CaptchaType.HCAPTCHA:
            field_name = "h-captcha-response"
        else:
            raise CaptchaUnsupported(
                f"Injeção de token não implementada para '{captcha.tipo}'."
            )

        context.evaluate(
            """
            ([fieldName, token, callbackName]) => {
                let el = document.querySelector(`[name="${fieldName}"]`);

                if (!el) {
                    el = document.createElement('textarea');
                    el.setAttribute('name', fieldName);
                    el.style.display = 'none';
                    document.body.appendChild(el);
                }

                el.value = token;
                el.innerHTML = token;

                if (callbackName && typeof window[callbackName] === 'function') {
                    window[callbackName](token);
                }
            }
            """,
            [field_name, token, captcha.action],
        )
