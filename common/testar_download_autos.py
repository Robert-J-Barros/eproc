"""
Testa isoladamente o download do "arquivo completo" (autos em PDF) de
UM processo já conhecido -- ver autos.py para o fluxo confirmado via
Burp Suite. Não mexe em nada do resto do crawler.

Uso:
    python testar_download_autos.py <numero_do_processo>

Ex.: python testar_download_autos.py 50113262120258210005

O PDF baixado (se tudo der certo) fica em
output/testar_download_autos/<numero_do_processo>.pdf
"""

import sys

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

from eproc_automation import abrir_sessao, validar_configuracao
from autos import baixar_autos_completo


def main():
    if len(sys.argv) != 2:
        raise SystemExit("Uso: python testar_download_autos.py <numero_do_processo>")

    numero_processo = sys.argv[1]
    validar_configuracao()

    with sync_playwright() as p:
        browser, context, page = abrir_sessao(p)

        try:
            destino = f"output/testar_download_autos/{numero_processo}.pdf"
            print(f"Abrindo processo {numero_processo} e agendando geração dos autos...")
            caminho = baixar_autos_completo(page, numero_processo, destino)
            print(f"OK -- autos baixados em: {caminho}")
        except PlaywrightTimeoutError as e:
            print(f"ERRO: timeout esperando um elemento -- {e}")
            page.screenshot(path="output/erro_testar_download_autos.png")
            raise
        except Exception as e:
            print(f"ERRO: {e}")
            page.screenshot(path="output/erro_testar_download_autos.png")
            raise
        finally:
            browser.close()


if __name__ == "__main__":
    main()
