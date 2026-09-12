"""
Diagnóstico passo a passo da navegação "empresa -> lista de processos"
(abrir_processos_da_empresa em crawler.py) para UMA única empresa --
tira screenshot e imprime a URL/título atual em cada checkpoint, pra
entender exatamente ONDE a navegação desvia para o 'Painel do
Advogado' em vez da lista de processos esperada.

Não usa aba auxiliar nem paginação em massa -- só busca um termo,
pega a primeira empresa encontrada, e segue o mesmo caminho de
abrir_processos_da_empresa manualmente, com paradas pra inspeção.

Uso:
    python diagnosticar_navegacao_empresa.py <termo de busca>

Ex.: python diagnosticar_navegacao_empresa.py "Comer cio"

Screenshots numerados ficam em output/diagnostico/.
"""

import os
import sys
from urllib.parse import urljoin

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

from eproc_automation import (
    abrir_sessao, abrir_consulta_processual, validar_configuracao,
    selecionar_tipo_pesquisa_nome_da_parte, CAMINHO_APP, URL_LOGIN,
)
from crawler import buscar_empresas_por_termo

DIR_SAIDA = "output/diagnostico"


def checkpoint(page, n: int, rotulo: str):
    os.makedirs(DIR_SAIDA, exist_ok=True)
    caminho = f"{DIR_SAIDA}/{n:02d}_{rotulo}.png"
    page.screenshot(path=caminho)
    titulo = page.title()
    print(f"[{n:02d}] {rotulo} -- url={page.url!r} titulo={titulo!r} -> {caminho}")


def main():
    if len(sys.argv) not in (2, 3):
        raise SystemExit(
            'Uso: python diagnosticar_navegacao_empresa.py "termo de busca" [--duas-abas]'
        )

    termo = sys.argv[1]
    duas_abas = len(sys.argv) == 3 and sys.argv[2] == "--duas-abas"
    validar_configuracao()

    with sync_playwright() as p:
        browser, context, page = abrir_sessao(p)

        # Modo --duas-abas: reproduz EXATAMENTE a arquitetura do
        # run_crawler.py (aba principal fazendo paginação + aba
        # auxiliar abrindo a empresa) -- diferente do modo padrão
        # (uma aba só), que já confirmamos funcionar. Se o bug só
        # aparece aqui, confirma que a causa é a segunda aba
        # concorrente, não o link/referer em si.
        pagina_alvo = context.new_page() if duas_abas else page
        if duas_abas:
            pagina_alvo.set_default_timeout(30_000)
            print("Modo --duas-abas: usando uma aba auxiliar pra abrir a empresa "
                  "(igual ao run_crawler.py), mantendo a aba principal na busca.\n")

        try:
            print("Abrindo Consulta Processual...")
            abrir_consulta_processual(page)
            selecionar_tipo_pesquisa_nome_da_parte(page)

            print(f"Buscando empresas para o termo {termo!r}...")
            empresas = buscar_empresas_por_termo(page, termo)
            if not empresas:
                raise SystemExit("Nenhuma empresa encontrada para esse termo -- tente outro.")

            empresa = empresas[0]
            print(f"\nUsando a primeira empresa encontrada: {empresa['nome']} ({empresa['id_pessoa']})")
            print(f"href capturado: {empresa.get('href')!r}\n")

            url_pagina_busca = page.url  # referer que abrir_processos_da_empresa usaria
            checkpoint(page, 1, "pagina_de_busca_antes_de_navegar")

            if not empresa.get("href"):
                raise SystemExit("Essa empresa não tem href -- não reproduz o bug investigado.")

            base_eproc = URL_LOGIN.rstrip("/") + CAMINHO_APP
            url_absoluta = urljoin(base_eproc, empresa["href"])
            print(f"URL absoluta calculada: {url_absoluta}")
            print(f"Referer que será enviado: {url_pagina_busca}\n")

            print(f"Navegando ({'aba auxiliar' if duas_abas else 'mesma aba'}, goto com referer)...")
            pagina_alvo.goto(url_absoluta, timeout=60_000, referer=url_pagina_busca)
            checkpoint(pagina_alvo, 2, "logo_apos_goto_antes_de_esperar")

            try:
                pagina_alvo.wait_for_load_state("networkidle", timeout=60_000)
            except PlaywrightTimeoutError:
                print("  (timeout esperando networkidle -- seguindo mesmo assim)")
            checkpoint(pagina_alvo, 3, "apos_networkidle")

            # Mais uma espera curta, caso haja redirecionamento client-side
            # (JS) que só dispara DEPOIS do networkidle -- é exatamente
            # essa janela que queremos capturar aqui.
            pagina_alvo.wait_for_timeout(3_000)
            checkpoint(pagina_alvo, 4, "3s_depois_do_networkidle")

            campo_nome_parte = pagina_alvo.query_selector("input[name='strNomeParte']")
            tem_campo_nome = campo_nome_parte is not None
            valor_nome = campo_nome_parte.input_value() if campo_nome_parte else None
            tem_msg_sem_classe = pagina_alvo.get_by_text("Nenhum processo (em movimento) encontrado").count() > 0
            tem_tabela = pagina_alvo.query_selector("#divInfraAreaTabela tbody tr") is not None
            tem_painel_advogado_heading = pagina_alvo.get_by_role("heading", name="Painel do Advogado").count() > 0
            tem_painel_advogado_texto = pagina_alvo.get_by_text("Painel do Advogado").count() > 0

            print("\n--- Estado final da página ---")
            print(f"  Campo 'Nome Parte' presente: {tem_campo_nome} (valor={valor_nome!r})")
            print(f"  Mensagem 'Nenhum processo... encontrado': {tem_msg_sem_classe}")
            print(f"  Tabela de resultados presente: {tem_tabela}")
            print(f"  Heading 'Painel do Advogado': {tem_painel_advogado_heading}")
            print(f"  Texto 'Painel do Advogado' em algum lugar da página: {tem_painel_advogado_texto}")

        except PlaywrightTimeoutError as e:
            print(f"ERRO: timeout esperando um elemento -- {e}")
            checkpoint(pagina_alvo, 99, "erro_timeout")
            raise
        finally:
            browser.close()


if __name__ == "__main__":
    main()
