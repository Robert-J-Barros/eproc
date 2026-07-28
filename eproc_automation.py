"""
Automação de consulta processual no e-Proc (TJRS) usando Playwright.

Reaproveita toda a lógica validada manualmente no Console do navegador
durante o desenvolvimento anterior no Power Automate Desktop:
  - Login em http://eproc1g.tjrs.jus.br
  - Seleção de "Tipo de Pesquisa" = Nome da Parte
  - Preenchimento do multiselect "Classe Processual" (MONITÓRIA +
    Embargos Parciais à Ação Monitória)

Requisitos:
    pip install playwright
    playwright install chromium

Uso:
    python eproc_automation.py
"""

import os

import pyotp
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# ============================================================
# CONFIGURAÇÃO -- lida de variáveis de ambiente (ver .env.example)
# ============================================================

URL_LOGIN = os.environ.get("EPROC_URL", "https://eproc1g.tjrs.jus.br")

USUARIO = os.environ.get("EPROC_USUARIO", "")
SENHA = os.environ.get("EPROC_SENHA", "")
TOTP_SECRET = os.environ.get("EPROC_TOTP_SECRET", "")

NOME_DA_PARTE = os.environ.get("EPROC_NOME_DA_PARTE", "NOME AQUI")

# Classes processuais a marcar no multiselect (value -> descrição)
# Os 2 primeiros já tinham sido confirmados visualmente. Os 2 últimos
# foram confirmados apenas como "value" via captura de tráfego (corpo
# real da consulta) -- não vimos o texto exibido na tela para eles,
# então preencha a descrição real assim que possível (ajuda na
# legibilidade dos logs, não afeta o funcionamento).
CLASSES_PROCESSUAIS = {
    "0000000028": "MONITÓRIA",
    "0000100137": "Embargos Parciais à Ação Monitória",
    "0000000094": "TODO: confirmar nome desta classe",
    "0000000029": "TODO: confirmar nome desta classe",
}

HEADLESS = os.environ.get("EPROC_HEADLESS", "true").lower() != "false"
TIMEOUT_PADRAO_MS = 30_000  # 30s de timeout para esperas
NAVEGACAO_TIMEOUT_MS = 60_000

# Onde salvar a sessão autenticada (cookies) entre execuções. Combinado
# com o checkbox "saveDevice" do Keycloak, permite pular o MFA nas
# próximas execuções -- só é pedido de novo quando a sessão expirar.
STORAGE_STATE_PATH = os.environ.get("EPROC_STORAGE_STATE", "output/storage_state.json")


def validar_configuracao():
    """Falha cedo e com mensagem clara se alguma variável obrigatória
    não foi definida -- melhor que descobrir isso no meio do login."""
    faltando = [
        nome for nome, valor in [
            ("EPROC_USUARIO", USUARIO),
            ("EPROC_SENHA", SENHA),
            ("EPROC_TOTP_SECRET", TOTP_SECRET),
        ] if not valor
    ]
    if faltando:
        raise SystemExit(
            f"ERRO: variáveis de ambiente faltando: {', '.join(faltando)}. "
            f"Copie .env.example para .env e preencha os valores, ou "
            f"exporte-as diretamente no ambiente."
        )


def gerar_codigo_mfa():
    """
    Gera o código TOTP atual (6 dígitos), igual ao app autenticador.

    Se o código estiver a menos de 5s de expirar (janela padrão de 30s),
    espera a próxima janela começar -- evita enviar um código que expira
    no meio da submissão do formulário.
    """
    import time

    totp = pyotp.TOTP(TOTP_SECRET)
    segundos_restantes = 30 - (int(time.time()) % 30)
    if segundos_restantes < 5:
        time.sleep(segundos_restantes + 1)
    return totp.now()


def ja_esta_logado(page) -> bool:
    """
    Verifica se a sessão restaurada (storage_state) ainda é válida --
    tenta ir para a URL de login e vê se foi redirecionado para o
    Keycloak (não logado) ou se caiu direto no painel (logado).
    """
    page.goto(URL_LOGIN, timeout=NAVEGACAO_TIMEOUT_MS)
    try:
        page.wait_for_selector("#username", timeout=5_000)
        return False  # apareceu tela de login -> não está logado
    except PlaywrightTimeoutError:
        return True  # não apareceu tela de login -> sessão ainda válida


def login(page):
    """
    Realiza o login no e-Proc, incluindo a etapa de MFA (TOTP).

    IMPORTANTE: o e-Proc não tem formulário de login próprio -- ele
    redireciona (302) para um servidor Keycloak (SSO/OpenID Connect) em
    keycloak-eks.tjrs.jus.br, com tokens dinâmicos (session_code,
    execution, state, nonce) que mudam a cada tentativa. O Playwright
    lida com os redirects e cookies automaticamente; só precisamos dos
    seletores dos formulários, confirmados via captura no Burp Suite:

      Formulário de usuário/senha (Keycloak):
        input#username, input#password, button#kc-login

      Formulário de código MFA (Keycloak, aparece após o login):
        input#otp, checkbox#saveDevice, button#kc-login
    """
    if ja_esta_logado(page):
        print("Sessão restaurada de execução anterior -- pulando login/MFA.")
        return

    page.wait_for_selector("#username", timeout=TIMEOUT_PADRAO_MS)
    page.fill("#username", USUARIO)
    page.fill("#password", SENHA)
    page.click("#kc-login")

    # --- Etapa de MFA (TOTP) ---
    page.wait_for_selector("#otp", timeout=TIMEOUT_PADRAO_MS)
    codigo = gerar_codigo_mfa()
    page.fill("#otp", codigo)

    # Marca "Não usar o 2FA neste dispositivo e navegador" -- assim,
    # combinado com o storage_state salvo ao final, as próximas
    # execuções pulam o MFA (ver ja_esta_logado / STORAGE_STATE_PATH).
    checkbox_save_device = page.query_selector("#saveDevice")
    if checkbox_save_device:
        checkbox_save_device.check()

    page.click("#kc-login")

    page.wait_for_load_state("networkidle", timeout=NAVEGACAO_TIMEOUT_MS)


def abrir_consulta_processual(page):
    """
    Navega até a tela de Consulta Processual.

    Estrutura do menu confirmada via captura no Burp Suite: é um menu
    colapsável (Bootstrap) com dois níveis --

      <a aria-label="Consulta Processual" data-target="#menu-ul-3">  (pai, expande o submenu)
        <ul id="menu-ul-3">
          <a aria-label="Consultar Processos" href="controlador.php?acao=processo_consultar&...">

    O href do link final contém um hash de sessão que muda a cada
    login, por isso clicamos pelo aria-label em vez de navegar direto
    pela URL. Uso de `.first` porque o layout responsivo do e-Proc
    duplica esse menu (versão desktop + versão mobile/offcanvas) no
    mesmo HTML.
    """
    page.locator("a[aria-label='Consulta Processual']").first.click()
    page.locator("a[aria-label='Consultar Processos']").first.click()

    page.wait_for_load_state("networkidle", timeout=NAVEGACAO_TIMEOUT_MS)


def selecionar_tipo_pesquisa_nome_da_parte(page):
    """
    Seleciona 'Nome da Parte' no dropdown Tipo de Pesquisa e preenche o
    campo de busca (autocomplete).

    Equivalente ao script JS validado manualmente no Console:
        select.value = 'NO';
        select.dispatchEvent(new Event('change', { bubbles: true }));

    Seletor do campo confirmado via captura de tráfego (Burp Suite) do
    corpo real da consulta: o campo se chama `strNomeParte` (name).
    IMPORTANTE: o valor deve manter os espaços normalmente -- o "+"
    visto no corpo da requisição (ex: "Agro+LT+DA") é só a codificação
    padrão de formulário para espaço; o Playwright já cuida disso
    sozinho ao usar page.fill() com a string normal (ex: "Agro LT DA").

    Esse campo é um autocomplete: ao digitar, o e-Proc dispara uma
    busca AJAX (acao_ajax=processos_consulta_por_nome_parte) e sugere
    nomes de partes já cadastradas. Aqui digitamos o nome e deixamos a
    consulta seguir com o texto literal (sem precisar clicar em uma
    sugestão específica) -- ajuste se seu fluxo exigir selecionar uma
    sugestão da lista.
    """
    page.wait_for_selector("#selTipoPesquisa", timeout=TIMEOUT_PADRAO_MS)
    page.select_option("#selTipoPesquisa", value="NO")  # NO = Nome da Parte

    campo_nome_parte = page.wait_for_selector(
        "input[name='strNomeParte']", timeout=TIMEOUT_PADRAO_MS
    )
    campo_nome_parte.fill(NOME_DA_PARTE)


def selecionar_classe_processual(page):
    """
    Abre o multiselect 'Classe Processual' e marca os itens configurados
    em CLASSES_PROCESSUAIS.

    Reaproveita 100% a lógica validada no Console:
        - container: #divClasseProcessual div.ms-parent.classeMultipleSelect
        - botão: .ms-choice
        - checkboxes: input[name="selectItemselIdClasse"][value="..."]

    NÃO filtramos pelo campo de busca (.ms-search) porque as classes
    configuradas em CLASSES_PROCESSUAIS podem ter nomes muito
    diferentes entre si (ex.: nem todas contêm "monit") -- alguns
    componentes desse tipo removem do DOM os itens que não batem com o
    filtro, então filtrar por um termo fixo arriscaria não encontrar
    algumas checkboxes. Selecionamos direto pelo atributo `value`, que
    é estável independente do texto exibido.
    """
    wrapper = page.wait_for_selector("#divClasseProcessual", timeout=TIMEOUT_PADRAO_MS)
    container = wrapper.wait_for_selector(
        "div.ms-parent.classeMultipleSelect", timeout=TIMEOUT_PADRAO_MS
    )

    botao = container.wait_for_selector(".ms-choice", timeout=TIMEOUT_PADRAO_MS)
    botao.click()

    # Espera o painel realmente abrir (display: block)
    container.wait_for_selector(".ms-drop", timeout=TIMEOUT_PADRAO_MS, state="visible")
    page.wait_for_timeout(300)  # pequena folga para o plugin renderizar a lista completa

    for value, descricao in CLASSES_PROCESSUAIS.items():
        checkbox = container.wait_for_selector(
            f'input[value="{value}"]', timeout=TIMEOUT_PADRAO_MS
        )
        if not checkbox.is_checked():
            checkbox.check(force=True)
        print(f"Classe processual marcada: {descricao} ({value})")


def consultar(page):
    """
    Clica no botão 'Consultar' final.

    O seletor original `text=Consultar` estava errado -- ele bate com
    QUALQUER texto contendo "Consultar" na página, incluindo itens do
    menu de navegação lateral (ex.: "Consultar Alvará Eletrônico
    Automatizado", "Consultar Processos"). O Playwright pegava o
    primeiro elemento encontrado (um <span> de menu escondido) e
    travava tentando clicar nele por 30s (erro confirmado em execução
    real: TimeoutError esperando esse span ficar visível).

    Correção: buscar pelo *role* de botão com nome exato "Consultar" --
    isso naturalmente ignora <span> de texto de menu (que não têm
    role=button) e só encontra <button>/<input type=submit> de verdade.

    Ajuste adicional (confirmado em execução real): a página do e-Proc
    tem DOIS botões "Consultar" com o MESMO id="sbmConsultar" -- um na
    barra de comandos superior (#divInfraBarraComandosSuperior) e outro
    na inferior (#divInfraBarraComandosInferior). IDs duplicados são
    inválidos em HTML, mas é assim que o e-Proc é montado (mesmo padrão
    de duplicação que já vimos em "info-vista-proc"). Os dois botões
    submetem o mesmo formulário, então usamos `.first` para pegar
    qualquer um deles sem erro de "strict mode violation".
    """
    page.get_by_role("button", name="Consultar", exact=True).first.click()
    page.wait_for_load_state("networkidle", timeout=NAVEGACAO_TIMEOUT_MS)


def abrir_sessao(p):
    """
    Abre o navegador, restaura sessão anterior se existir, e garante
    login feito. Retorna (browser, context, page) prontos para uso.
    Reaproveitada tanto pela consulta única (main()) quanto pelo
    crawler de varredura em massa (run_crawler.py).

    EPROC_FORCAR_NOVO_LOGIN=true (no .env) ignora o storage_state
    salvo, mesmo que exista -- o contexto nasce "limpo" (sem cookies
    de sessões anteriores), equivalente a uma aba anônima/incógnito.
    Isso força login + MFA (TOTP) do zero em toda execução.
    """
    os.makedirs("output", exist_ok=True)
    browser = p.chromium.launch(headless=HEADLESS)

    forcar_novo_login = os.environ.get("EPROC_FORCAR_NOVO_LOGIN", "false").lower() == "true"

    if not forcar_novo_login and os.path.exists(STORAGE_STATE_PATH):
        context = browser.new_context(storage_state=STORAGE_STATE_PATH)
    else:
        context = browser.new_context()

    page = context.new_page()
    page.set_default_timeout(TIMEOUT_PADRAO_MS)

    # Captura e loga qualquer alert()/confirm()/prompt() disparado pelo
    # JavaScript da página -- sem isso, o Playwright descarta esses
    # diálogos silenciosamente por padrão, e a gente nunca fica sabendo
    # que uma ação foi bloqueada por uma validação client-side (ex.:
    # "entidades com muitos processos não podem ser consultadas").
    def _log_dialog(dialog):
        print(f"[DIÁLOGO JS] tipo={dialog.type} mensagem={dialog.message!r}")
        dialog.dismiss()

    page.on("dialog", _log_dialog)

    print("Fazendo login...")
    login(page)
    context.storage_state(path=STORAGE_STATE_PATH)

    return browser, context, page


def main():
    validar_configuracao()

    with sync_playwright() as p:
        browser, context, page = abrir_sessao(p)

        try:
            print("Abrindo Consulta Processual...")
            abrir_consulta_processual(page)

            print("Selecionando Tipo de Pesquisa = Nome da Parte...")
            selecionar_tipo_pesquisa_nome_da_parte(page)

            print("Selecionando Classe Processual...")
            selecionar_classe_processual(page)

            print("Executando consulta...")
            consultar(page)

            print("Consulta concluída com sucesso.")

            # TODO: adicionar aqui a extração dos resultados
            # (equivalente às ações "Obter caixas de seleção marcadas"
            # / captura de tabela de resultados do Power Automate).

        except PlaywrightTimeoutError as e:
            print(f"ERRO: timeout esperando um elemento -- {e}")
            page.screenshot(path="output/erro_timeout.png")
            raise
        except Exception as e:
            print(f"ERRO inesperado: {e}")
            page.screenshot(path="output/erro_geral.png")
            raise
        finally:
            browser.close()


if __name__ == "__main__":
    main()