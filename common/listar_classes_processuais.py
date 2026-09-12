"""
Lista TODAS as classes disponíveis no multiselect "Classe Processual"
da tela de Consulta Processual (value + descrição de cada checkbox) --
útil para descobrir os `value`s corretos de um tribunal antes de
configurar EPROC_CLASSES_PROCESSUAIS no .env dele (ver docstring de
selecionar_classe_processual em eproc_automation.py: o `value` de cada
classe é específico de CADA tribunal, não um código nacional).

Uso (dentro do container do tribunal desejado):
    docker compose exec eproc-automation python listar_classes_processuais.py

Opcionalmente filtra por um termo (case-insensitive, busca na
descrição) pra não ter que rolar uma lista enorme:
    docker compose exec eproc-automation python listar_classes_processuais.py execu
"""

import sys

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

from eproc_automation import (
    abrir_sessao, abrir_consulta_processual, validar_configuracao,
    selecionar_tipo_pesquisa_nome_da_parte, TIMEOUT_PADRAO_MS,
)


def listar_classes(page) -> list[dict]:
    """
    Abre o multiselect Classe Processual (mesmos seletores usados em
    selecionar_classe_processual) e lê value+descrição de cada
    checkbox, sem marcar nenhum -- só leitura.
    """
    wrapper = page.wait_for_selector("#divClasseProcessual", timeout=TIMEOUT_PADRAO_MS)
    container = wrapper.wait_for_selector(
        "div.ms-parent.classeMultipleSelect", timeout=TIMEOUT_PADRAO_MS
    )

    botao = container.wait_for_selector(".ms-choice", timeout=TIMEOUT_PADRAO_MS)
    botao.click()

    container.wait_for_selector(".ms-drop", timeout=TIMEOUT_PADRAO_MS, state="visible")
    page.wait_for_timeout(300)  # folga pro plugin renderizar a lista completa

    itens = []
    for checkbox in container.query_selector_all('input[name="selectItemselIdClasse"]'):
        value = checkbox.get_attribute("value")
        if not value:
            continue
        # A descrição normalmente fica no <label> que envolve o
        # checkbox, ou num <span> irmão -- tentamos os dois padrões
        # mais comuns desse plugin de multiselect.
        label = checkbox.evaluate_handle("el => el.closest('label')").as_element()
        texto = label.inner_text().strip() if label else ""
        itens.append({"value": value, "descricao": texto})

    return itens


def main():
    validar_configuracao()
    filtro = sys.argv[1].strip().upper() if len(sys.argv) > 1 else None

    with sync_playwright() as p:
        browser, context, page = abrir_sessao(p)

        try:
            print("Abrindo Consulta Processual...")
            abrir_consulta_processual(page)

            # IMPORTANTE: #divClasseProcessual existe no DOM desde o
            # carregamento da página, mas fica OCULTO (display: none)
            # até o Tipo de Pesquisa ser trocado para "Nome da Parte" --
            # confirmado em execução real (timeout esperando o div ficar
            # visível, mesmo ele já estando "resolvido" pelo Playwright).
            # O mesmo select_option("NO") + evento "change" usado no
            # fluxo normal de consulta é o que revela esse bloco.
            print("Selecionando Tipo de Pesquisa = Nome da Parte (revela o filtro de Classe)...")
            selecionar_tipo_pesquisa_nome_da_parte(page)

            print("Lendo classes disponíveis no multiselect...\n")
            classes = listar_classes(page)

            if filtro:
                classes = [c for c in classes if filtro in c["descricao"].upper()]

            if not classes:
                print("Nenhuma classe encontrada" + (f" contendo {filtro!r}." if filtro else "."))
                return

            print(f"{len(classes)} classe(s) encontrada(s):\n")
            for c in classes:
                print(f"  {c['value']}:{c['descricao']}")

            print(
                "\nPra usar, cole no .env deste tribunal em "
                "EPROC_CLASSES_PROCESSUAIS, formato "
                "'value1:Descrição 1,value2:Descrição 2'."
            )

        except PlaywrightTimeoutError as e:
            print(f"ERRO: timeout esperando um elemento -- {e}")
            page.screenshot(path="output/erro_listar_classes.png")
            raise
        finally:
            browser.close()


if __name__ == "__main__":
    main()
