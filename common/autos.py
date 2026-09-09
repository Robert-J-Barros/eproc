"""
Download do "arquivo completo" (autos em PDF) de um processo já
conhecido pelo número -- fluxo confirmado via captura de tráfego no
Burp Suite (não adivinhado): login SSO/Keycloak -> abrir o processo
pelo número -> agendar a geração assíncrona do arquivo completo ->
polling até o servidor terminar -> download do PDF pronto.

O que NÃO está confirmado ainda (fica pra depois, quando tivermos uma
captura equivalente): a busca de processos por CPF/CNPJ de um cliente.
Este módulo assume que o número do processo já é conhecido (ex.: veio
do DataJud), então não precisa passar pela tela de busca.

Uso isolado (fora do pipeline principal, só pra validar o fluxo):
    python testar_download_autos.py <numero_do_processo>
"""

import re
import time
from pathlib import Path

from eproc_automation import NAVEGACAO_TIMEOUT_MS, URL_LOGIN, verificar_e_resolver_captcha

# Confirmado via Burp Suite: subdomínio dedicado ao download dos
# arquivos gerados, mesma sessão/cookies do domínio principal.
URL_DOWNLOAD = URL_LOGIN.replace("eproc1g.tjrs.jus.br", "eproc1g-download.tjrs.jus.br")

_HASH_RE = re.compile(r"[?&]hash=([0-9a-f]+)")
_ID_SESSAO_RE = re.compile(r"numIdSessao['\"=:]+(\d+)")
_ID_BATCH_RE = re.compile(r"numIdProcessosBatch['\"=:]+(\d+)")
_FILE_RE = re.compile(r"[?&]file=([^&\"'\s]+)")


def _extrair_hash(page) -> str:
    """
    Extrai o token anti-CSRF "hash" da página atual. Confirmado via
    Burp Suite: todo link/ação do e-Proc carrega esse parâmetro, e ele
    muda a cada requisição -- não dá pra fixar como constante, tem que
    reler da página anterior antes de montar a próxima URL.
    """
    m = _HASH_RE.search(page.content())
    if not m:
        raise RuntimeError(
            "Não encontrei o parâmetro 'hash' na página atual -- a sessão "
            "pode ter expirado ou a estrutura da página mudou desde a "
            "captura original no Burp Suite."
        )
    return m.group(1)


def abrir_processo_por_numero(page, numero_processo: str) -> None:
    """
    Abre a "vista" de um processo já conhecido, direto pelo número --
    NÃO passa pela tela de busca (o nome do campo do formulário de
    busca por número ainda não foi confirmado via captura). Aposta
    válida porque o endpoint `processo_selecionar` já aceita o número
    como parâmetro de URL, funcionando como link direto.

    Confirmado via Burp Suite:
        GET controlador.php?acao=processo_selecionar&num_processo=...&hash=...
    """
    hash_atual = _extrair_hash(page)
    page.goto(
        f"{URL_LOGIN}/eproc/controlador.php?acao=processo_selecionar"
        f"&num_processo={numero_processo}&hash={hash_atual}",
        timeout=NAVEGACAO_TIMEOUT_MS,
    )
    page.wait_for_load_state("networkidle", timeout=NAVEGACAO_TIMEOUT_MS)
    verificar_e_resolver_captcha(page, onde="abrir processo por número")


def agendar_geracao_autos_completo(page, numero_processo: str) -> tuple[str, str]:
    """
    Dispara a geração assíncrona do arquivo completo (autos em PDF) do
    processo já aberto. Confirmado via Burp Suite:
        GET controlador.php?acao=agendar_geracao_arquivo_processo_completo
            &num_processo=...&uf=RS&sin_chave=N&hash=...

    Retorna (numIdSessao, numIdProcessosBatch), usados no polling do
    download. A captura original não mostrou o corpo da resposta desta
    etapa, então extraímos os dois valores via regex do HTML/JS
    renderizado -- cobre tanto uma resposta JSON quanto uma página que
    já embute um poller em JavaScript, sem assumir uma estrutura fixa.
    """
    hash_atual = _extrair_hash(page)
    page.goto(
        f"{URL_LOGIN}/eproc/controlador.php?acao=agendar_geracao_arquivo_processo_completo"
        f"&num_processo={numero_processo}&uf=RS&sin_chave=N&hash={hash_atual}",
        timeout=NAVEGACAO_TIMEOUT_MS,
    )
    page.wait_for_load_state("networkidle", timeout=NAVEGACAO_TIMEOUT_MS)
    verificar_e_resolver_captcha(page, onde="agendar geração do arquivo completo")

    conteudo = page.content()
    id_sessao = _ID_SESSAO_RE.search(conteudo)
    id_batch = _ID_BATCH_RE.search(conteudo)
    if not (id_sessao and id_batch):
        raise RuntimeError(
            "Não encontrei numIdSessao/numIdProcessosBatch na página após "
            "agendar a geração do arquivo completo -- a estrutura real da "
            "resposta pode ser diferente do que a regex espera. Rode com "
            "EPROC_HEADLESS=false e inspecione a página manualmente, ou "
            "salve o HTML (page.content()) pra ajustar os padrões."
        )
    return id_sessao.group(1), id_batch.group(1)


def aguardar_e_baixar_autos(
    page,
    numero_processo: str,
    num_id_sessao: str,
    num_id_processos_batch: str,
    destino: str,
    intervalo_polling_s: int = 5,
    timeout_total_s: int = 300,
) -> str:
    """
    Faz polling em `download_completo_download_pronto_enviar` (no
    subdomínio dedicado eproc1g-download.tjrs.jus.br) até a resposta
    trazer o parâmetro `file=` -- sinal de que o arquivo terminou de
    ser gerado no servidor -- e então baixa o PDF pra `destino`.

    Confirmado via Burp Suite, incluindo o parâmetro fixo `zip=S` (não
    sabemos se controla o formato do arquivo final ou é constante do
    fluxo; mantido igual à captura original).
    """
    hash_atual = _extrair_hash(page)
    inicio = time.time()
    file_param = None

    while time.time() - inicio < timeout_total_s:
        resposta = page.request.get(
            f"{URL_DOWNLOAD}/eproc/controlador.php",
            params={
                "acao": "download_completo_download_pronto_enviar",
                "zip": "S",
                "numIdSessao": num_id_sessao,
                "numIdProcessosBatch": num_id_processos_batch,
                "hash": hash_atual,
            },
            timeout=NAVEGACAO_TIMEOUT_MS,
        )
        m = _FILE_RE.search(resposta.text())
        if m:
            file_param = m.group(1)
            break
        time.sleep(intervalo_polling_s)

    if file_param is None:
        raise TimeoutError(
            f"Autos de {numero_processo} não ficaram prontos em "
            f"{timeout_total_s}s de polling."
        )

    resposta_arquivo = page.request.get(
        f"{URL_DOWNLOAD}/eproc/controlador.php",
        params={
            "acao": "download_completo_download_pronto_enviar",
            "file": file_param,
            "numIdSessao": num_id_sessao,
            "numIdProcessosBatch": num_id_processos_batch,
            "hash": hash_atual,
        },
        timeout=NAVEGACAO_TIMEOUT_MS,
    )
    if not resposta_arquivo.ok:
        raise RuntimeError(
            f"Falha ao baixar autos de {numero_processo}: HTTP {resposta_arquivo.status}"
        )

    caminho = Path(destino)
    caminho.parent.mkdir(parents=True, exist_ok=True)
    caminho.write_bytes(resposta_arquivo.body())
    return str(caminho)


def baixar_autos_completo(page, numero_processo: str, destino: str) -> str:
    """
    Função de conveniência: encadeia todo o fluxo confirmado via Burp
    Suite -- abrir o processo pelo número, agendar a geração do
    arquivo completo e baixar o PDF assim que ficar pronto.
    """
    abrir_processo_por_numero(page, numero_processo)
    num_id_sessao, num_id_processos_batch = agendar_geracao_autos_completo(page, numero_processo)
    return aguardar_e_baixar_autos(
        page, numero_processo, num_id_sessao, num_id_processos_batch, destino
    )
