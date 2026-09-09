"""
Script de DIAGNÓSTICO -- não faz parte do fluxo de produção do
crawler. Objetivo único: descobrir, numa sessão logada de verdade, a
estrutura real de duas telas do e-Proc que o código atual nunca tocou:

  1. As opções do dropdown "Tipo de Pesquisa" (#selTipoPesquisa) na
     Consulta Processual -- hoje só sabemos o valor "NO" (Nome da
     Parte, ver `selecionar_tipo_pesquisa_nome_da_parte` em
     eproc_automation.py); não sabemos o valor do modo "Documento
     (CPF/CNPJ)".
  2. A tela de detalhe de um processo -- hoje só extraímos campos de
     cabeçalho e "Informações Adicionais" (ver `extrair_detalhe_processo`
     em crawler.py); não sabemos onde fica o link/aba de autos/documentos
     nem como o download funciona (nova aba? iframe? link direto?).

NÃO tenta baixar nenhum PDF de verdade -- só relatar o que existe, pra
quem for escrever a implementação real (busca por CPF/CNPJ + download
dos autos) trabalhar com seletores confirmados em vez de chutados.

Tudo que este script descobre é salvo em output/diagnostico_cliente/
(screenshots .png + HTML bruto .html + um resumo.txt com o que foi
encontrado em cada etapa). Nada é impresso só no console -- se o
processo travar no meio, os arquivos já salvos até ali continuam
válidos.

Uso:
    python diagnostico_cliente.py <cpf_ou_cnpj_de_teste>

Requer as mesmas variáveis de ambiente já usadas pelo crawler normal
(EPROC_USUARIO, EPROC_SENHA, EPROC_TOTP_SECRET, NOPECHA_API_KEY etc. --
ver .env.example) -- reaproveita a mesma sessão/login.
"""

import os
import sys
from datetime import datetime

from playwright.sync_api import sync_playwright

from eproc_automation import (
    TIMEOUT_PADRAO_MS,
    abrir_consulta_processual,
    abrir_sessao,
    validar_configuracao,
    verificar_e_resolver_captcha,
)

DIR_SAIDA = "output/diagnostico_cliente"

_linhas_resumo: list[str] = []


def _log(mensagem: str):
    """Imprime no console E acumula pro resumo.txt final."""
    print(mensagem)
    _linhas_resumo.append(mensagem)


def _salvar_texto(nome_arquivo: str, conteudo: str):
    caminho = os.path.join(DIR_SAIDA, nome_arquivo)
    with open(caminho, "w", encoding="utf-8") as f:
        f.write(conteudo)
    _log(f"  -> salvo {caminho} ({len(conteudo)} chars)")


def _salvar_screenshot(page, nome_arquivo: str):
    caminho = os.path.join(DIR_SAIDA, nome_arquivo)
    page.screenshot(path=caminho, full_page=True)
    _log(f"  -> screenshot salvo {caminho}")


def _dump_opcoes_select(page, seletor: str) -> list[tuple[str, str]]:
    """Retorna [(value, texto_visivel), ...] de um <select>."""
    return page.eval_on_selector_all(
        seletor,
        "opts => opts.map(o => [o.value, o.textContent.trim()])",
    )


def _dump_campos_visiveis(page) -> list[dict]:
    """
    Lista todo input/select visível na página -- usado pra descobrir o
    id/name do campo de CPF/CNPJ depois de selecionar o modo Documento,
    já que só conhecemos o nome do campo do modo Nome da Parte
    (strNomeParte).
    """
    return page.eval_on_selector_all(
        "input, select",
        """els => els.map(e => ({
            tag: e.tagName,
            id: e.id,
            name: e.name,
            type: e.type || null,
            visivel: e.offsetParent !== null,
        })).filter(e => e.visivel)""",
    )


def _procurar_pistas_de_documentos(html: str) -> list[str]:
    """
    Varre o HTML bruto da tela de detalhe do processo procurando
    qualquer indício de aba/link de autos/documentos -- não confia em
    nenhum seletor específico (não sabemos qual existe ainda), só
    procura as palavras-chave relevantes perto de tags <a>/<button>/
    aria-label, pra dar um ponto de partida pra inspeção manual do
    resumo.txt.
    """
    import re

    achados = []
    palavras = ["autos", "documento", "peça", "peca", "anexo", "pdf", "arquivo"]
    for match in re.finditer(r"<a[^>]*>.*?</a>|<button[^>]*>.*?</button>", html, re.IGNORECASE | re.DOTALL):
        trecho = match.group(0)
        if any(p in trecho.lower() for p in palavras):
            achados.append(trecho.strip()[:300])
    return achados


def main():
    if len(sys.argv) < 2:
        print("Uso: python diagnostico_cliente.py <cpf_ou_cnpj_de_teste>")
        sys.exit(1)

    cpf_cnpj_teste = sys.argv[1]

    os.makedirs(DIR_SAIDA, exist_ok=True)
    validar_configuracao()

    _log(f"=== Diagnóstico iniciado em {datetime.now().isoformat()} ===")
    _log(f"CPF/CNPJ de teste: {cpf_cnpj_teste}")

    with sync_playwright() as p:
        browser, context, page = abrir_sessao(p)
        try:
            _log("\n--- Etapa 1: abrir Consulta Processual ---")
            abrir_consulta_processual(page)
            _salvar_screenshot(page, "01_consulta_processual.png")

            _log("\n--- Etapa 2: dump das opções de #selTipoPesquisa ---")
            try:
                opcoes = _dump_opcoes_select(page, "#selTipoPesquisa")
                for valor, texto in opcoes:
                    _log(f"  value={valor!r}  texto={texto!r}")
            except Exception as e:
                _log(f"  ERRO ao ler #selTipoPesquisa: {e!r}")
                opcoes = []

            candidata = None
            for valor, texto in opcoes:
                texto_lower = texto.lower()
                if "documento" in texto_lower or "cpf" in texto_lower or "cnpj" in texto_lower:
                    candidata = (valor, texto)
                    break

            if candidata is None:
                _log(
                    "\n  Nenhuma opção com 'Documento'/'CPF'/'CNPJ' no texto visível. "
                    "Reveja a lista de opções acima manualmente -- pode estar com um "
                    "rótulo diferente do esperado."
                )
            else:
                valor, texto = candidata
                _log(f"\n  Opção candidata encontrada: value={valor!r} texto={texto!r}")

                _log("\n--- Etapa 3: selecionar o modo Documento e listar campos visíveis ---")
                page.select_option("#selTipoPesquisa", value=valor)
                page.wait_for_timeout(500)  # dá tempo de qualquer JS trocar os campos do form
                _salvar_screenshot(page, "02_modo_documento_selecionado.png")

                campos = _dump_campos_visiveis(page)
                _log("  Campos visíveis no formulário após selecionar o modo:")
                for campo in campos:
                    _log(f"    {campo}")

                campo_documento = None
                for campo in campos:
                    alvo = f"{campo.get('id', '')} {campo.get('name', '')}".lower()
                    if "documento" in alvo or "cpf" in alvo or "cnpj" in alvo:
                        campo_documento = campo
                        break

                if campo_documento is None:
                    _log(
                        "\n  Não achei automaticamente um campo com 'documento'/'cpf'/'cnpj' "
                        "no id/name -- reveja a lista de campos acima manualmente pra achar o certo."
                    )
                else:
                    _log(f"\n  Campo candidato pro CPF/CNPJ: {campo_documento}")
                    seletor_campo = (
                        f"#{campo_documento['id']}" if campo_documento.get("id")
                        else f"[name='{campo_documento['name']}']"
                    )

                    _log("\n--- Etapa 4: preencher e consultar ---")
                    page.fill(seletor_campo, "")
                    page.type(seletor_campo, cpf_cnpj_teste, delay=100)
                    verificar_e_resolver_captcha(page, onde="antes de consultar por documento")
                    page.get_by_role("button", name="Consultar", exact=True).first.click()
                    page.wait_for_load_state("networkidle", timeout=TIMEOUT_PADRAO_MS)
                    verificar_e_resolver_captcha(page, onde="após consultar por documento")
                    page.wait_for_timeout(1000)

                    _salvar_screenshot(page, "03_resultado_busca_documento.png")
                    _salvar_texto("03_resultado_busca_documento.html", page.content())

                    _log("\n--- Etapa 5: abrir o primeiro processo encontrado (se houver) ---")
                    primeiro_link = page.locator(
                        "#divInfraAreaTabela tbody tr:first-child a"
                    ).first
                    if primeiro_link.count() == 0:
                        _log(
                            "  Nenhum link de processo encontrado na tabela de resultados -- "
                            "verifique 03_resultado_busca_documento.png/.html manualmente "
                            "(pode ser que o CPF/CNPJ de teste não tenha processos, ou que a "
                            "busca tenha caído numa tela de erro)."
                        )
                    else:
                        with context.expect_page(timeout=5000) as nova_aba_info:
                            primeiro_link.click()
                        try:
                            pagina_processo = nova_aba_info.value
                            pagina_processo.wait_for_load_state("networkidle", timeout=TIMEOUT_PADRAO_MS)
                        except Exception:
                            # Não abriu em aba nova -- deve ter navegado na mesma página.
                            pagina_processo = page
                            pagina_processo.wait_for_load_state("networkidle", timeout=TIMEOUT_PADRAO_MS)

                        verificar_e_resolver_captcha(pagina_processo, onde="detalhe do processo")
                        html_processo = pagina_processo.content()
                        _salvar_screenshot(pagina_processo, "04_detalhe_processo.png")
                        _salvar_texto("04_detalhe_processo.html", html_processo)

                        _log("\n--- Etapa 6: procurar pistas de autos/documentos no HTML ---")
                        pistas = _procurar_pistas_de_documentos(html_processo)
                        if not pistas:
                            _log(
                                "  Nenhuma pista óbvia encontrada nas tags <a>/<button> -- "
                                "abra 04_detalhe_processo.html manualmente e procure por "
                                "'autos', 'documento', 'peça' no HTML (pode estar numa aba "
                                "carregada via AJAX que não veio no HTML inicial)."
                            )
                        else:
                            _log(f"  {len(pistas)} trecho(s) com palavras-chave relevantes:")
                            for trecho in pistas:
                                _log(f"    {trecho}")

        finally:
            context.close()
            browser.close()

    _salvar_texto("resumo.txt", "\n".join(_linhas_resumo))
    _log(f"\n=== Diagnóstico concluído -- veja tudo em {DIR_SAIDA}/ ===")


if __name__ == "__main__":
    main()
