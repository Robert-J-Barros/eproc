"""
Automação de consulta processual no e-Proc usando Playwright.

Nasceu para o TJRS (reaproveitando a lógica validada manualmente no
Console do navegador durante o desenvolvimento anterior no Power
Automate Desktop), mas o login já suporta qualquer tribunal e-Proc
apontado por EPROC_URL -- ver docstring de login() para os dois fluxos
possíveis (Keycloak/SSO ou formulário nativo). Confirmado em uso real
contra o TJTO além do TJRS.

  - Login em EPROC_URL (TJRS: http://eproc1g.tjrs.jus.br)
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

from captcha import CaptchaDetector, CaptchaSolver, CaptchaSolverError, NopeCHAClient, NopeCHAProvider

# ============================================================
# CONFIGURAÇÃO -- lida de variáveis de ambiente (ver .env.example)
# ============================================================

URL_LOGIN = os.environ.get("EPROC_URL", "https://eproc1g.tjrs.jus.br")

# Caminho onde a aplicação e-Proc roda dentro do domínio do tribunal --
# usado pra montar URLs absolutas a partir de href relativos (ver
# crawler.py::abrir_processos_da_empresa). IMPORTANTE (confirmado ao
# vivo contra o TJTO): esse caminho é específico de CADA TRIBUNAL, não
# um padrão nacional -- é "/eproc/" no TJRS mas
# "/eprocV2_prod_1grau/" no TJTO. Configurável via EPROC_CAMINHO_APP
# pra não quebrar ao trocar de tribunal.
CAMINHO_APP = "/" + os.environ.get("EPROC_CAMINHO_APP", "eproc").strip("/") + "/"

USUARIO = os.environ.get("EPROC_USUARIO", "")
SENHA = os.environ.get("EPROC_SENHA", "")
TOTP_SECRET = os.environ.get("EPROC_TOTP_SECRET", "")

NOME_DA_PARTE = os.environ.get("EPROC_NOME_DA_PARTE", "NOME AQUI")

# Classes processuais a marcar no multiselect (value -> descrição).
#
# IMPORTANTE (confirmado ao vivo contra o TJTO): os `value` de cada
# classe são específicos de CADA TRIBUNAL, não um código nacional
# compartilhado -- "Embargos Parciais à Ação Monitória" é
# "0000100137" no TJRS mas "0000002840" no TJTO. Por isso esse mapa
# vem do .env (EPROC_CLASSES_PROCESSUAIS), não fixo no código: cada
# TJXX/.env configura os values corretos pro seu próprio tribunal, sem
# precisar tocar em common/. Sem essa variável, cai no padrão abaixo
# (os values confirmados do TJRS, comportamento de sempre).
#
# Formato: "value1:Descrição 1,value2:Descrição 2" (vírgula separa
# classes, dois-pontos separa value de descrição).
_CLASSES_PROCESSUAIS_PADRAO = {
    "0000000028": "MONITÓRIA",
    "0000100137": "Embargos Parciais à Ação Monitória",
    "0000000094": "EXECUÇÃO DE TÍTULO EXTRAJUDICIAL",
    "0000000029": "PROCEDIMENTO COMUM CÍVEL",
}


def _carregar_classes_processuais() -> dict[str, str]:
    bruto = os.environ.get("EPROC_CLASSES_PROCESSUAIS", "").strip()
    if not bruto:
        return dict(_CLASSES_PROCESSUAIS_PADRAO)

    classes = {}
    for par in bruto.split(","):
        par = par.strip()
        if not par:
            continue
        value, _, descricao = par.partition(":")
        classes[value.strip()] = descricao.strip() or value.strip()
    return classes


CLASSES_PROCESSUAIS = _carregar_classes_processuais()

HEADLESS = os.environ.get("EPROC_HEADLESS", "true").lower() != "false"
TIMEOUT_PADRAO_MS = 30_000  # 30s de timeout para esperas
NAVEGACAO_TIMEOUT_MS = 60_000

# Onde salvar a sessão autenticada (cookies) entre execuções. Combinado
# com o checkbox "saveDevice" do Keycloak, permite pular o MFA nas
# próximas execuções -- só é pedido de novo quando a sessão expirar.
STORAGE_STATE_PATH = os.environ.get("EPROC_STORAGE_STATE", "output/storage_state.json")

# Chave de API da NopeCHA, usada apenas SE um captcha for detectado em
# alguma tela (evento raro no e-Proc). Sem captcha na tela, esta
# variável nunca é usada -- ver verificar_e_resolver_captcha().
# Pode ficar vazia; o detector continua rodando normalmente, só que se
# um captcha realmente aparecer, o NopeCHAClient vai falhar com uma
# mensagem clara pedindo para configurar EPROC_NOPECHA_API_KEY.
NOPECHA_API_KEY = os.environ.get("EPROC_NOPECHA_API_KEY", "").strip() or None


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


def verificar_e_resolver_captcha(page, onde: str = ""):
    """
    Checagem rápida (sem espera) de captcha na tela atual -- e-Proc e o
    Keycloak raramente exibem um, então isso NÃO fica em polling: só
    olha o DOM uma vez, na hora em que é chamada, e volta na mesma hora
    se não achar nada. Custo de chamar isso "só por garantia" em vários
    pontos do fluxo é desprezível (uma consulta de seletor no DOM).

    Se encontrar um captcha, tenta resolver via NopeCHA (API de token).
    Isso SIM pode levar alguns segundos (chamada de rede + polling do
    resultado), mas só acontece nesse caso raro.

    `onde` é só um rótulo pro log, pra saber em que etapa do fluxo o
    captcha apareceu (ex.: "login", "consulta").

    Levanta CaptchaSolverError se o captcha for detectado mas não puder
    ser resolvido -- nesse caso o fluxo normal (login/consulta) não vai
    conseguir prosseguir mesmo, então é melhor falhar aqui com uma
    mensagem clara do que travar depois num timeout genérico.
    """
    captcha = CaptchaDetector().detect(page)

    if captcha is None:
        return None

    rotulo = f" ({onde})" if onde else ""
    print(f"[CAPTCHA] Detectado{rotulo}: {captcha.tipo.value} -- tentando resolver via NopeCHA...")

    try:
        solver = CaptchaSolver(NopeCHAProvider(NopeCHAClient(api_key=NOPECHA_API_KEY)))
        resultado = solver.solve(page, captcha)
    except CaptchaSolverError as e:
        print(f"[CAPTCHA] Falha ao resolver{rotulo}: {e}")
        raise

    print(
        f"[CAPTCHA] Resolvido{rotulo} em {resultado.elapsed_time:.1f}s "
        f"via {resultado.provider}."
    )
    return resultado


def _verificar_mfa_aceito(page, onde: str = ""):
    """
    Confere se o código MFA (TOTP) enviado foi ACEITO pelo e-Proc.

    BUG CORRIGIDO (confirmado em execução real contra o TJRS, screenshot
    output/erro_ciclo_geral.png): quando o código é rejeitado, a página
    NÃO navega -- ela recarrega o MESMO formulário com um erro inline
    ("Código autenticador inválido."), então `wait_for_load_state
    ("networkidle")` (chamado logo em seguida em _login_keycloak/
    _login_nativo) considera a etapa "concluída" mesmo com o MFA tendo
    falhado. Sem essa checagem, login() seguia adiante como se tivesse
    logado com sucesso, salvava esse storage_state QUEBRADO, e só
    quebrava 3 passos depois com um erro de timeout completamente sem
    relação aparente (tentando clicar no menu "Consulta Processual",
    que não existe numa sessão não autenticada) -- em vez de apontar
    direto pro problema real (código MFA rejeitado).

    Checagem rápida sem polling (mesmo espírito de
    verificar_e_resolver_captcha): o erro, quando existe, já está
    renderizado no momento em que chamamos isso (logo após o
    wait_for_load_state que já esperou a resposta assentar).
    """
    erro = page.get_by_text("Código autenticador inválido").or_(
        page.get_by_text("código informado é inválido")
    )
    if erro.count() == 0:
        return

    rotulo = f" ({onde})" if onde else ""
    raise RuntimeError(
        f"Código MFA (TOTP) rejeitado pelo e-Proc{rotulo} ('Código "
        f"autenticador inválido'). Confira se EPROC_TOTP_SECRET está "
        f"correto e corresponde ao autenticador cadastrado nesta conta "
        f"-- se o app/dispositivo 2FA foi trocado ou recadastrado "
        f"recentemente, o secret antigo para de funcionar. Também "
        f"pode ser corrida (código expirou entre gerar e submeter); "
        f"tente rodar de novo antes de mexer na configuração."
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
    tenta ir para a URL de login e vê se apareceu ALGUMA tela de login
    conhecida (Keycloak/TJRS ou nativa do e-Proc/TJTO -- ver login())
    ou se caiu direto no painel (logado).

    IMPORTANTE: nem todo tribunal usa o SSO Keycloak -- checar só
    `#username` faria essa função sempre concluir "já logado" nos
    tribunais com login nativo (o campo lá é `#txtUsuario`), mesmo numa
    sessão nova sem cookie nenhum. Por isso checamos os dois seletores
    conhecidos, não só um (mesmo ajuste já confirmado necessário ao
    testar contra o TJTO).
    """
    page.goto(URL_LOGIN, timeout=NAVEGACAO_TIMEOUT_MS)
    verificar_e_resolver_captcha(page, onde="tela de login")
    campo_login = page.locator("#username").or_(page.locator("#txtUsuario"))
    try:
        campo_login.first.wait_for(state="visible", timeout=5_000)
        return False  # apareceu tela de login -> não está logado
    except PlaywrightTimeoutError:
        pass  # não apareceu tela de login -> sessão ainda válida

    # BUG CORRIGIDO (confirmado em execução real contra o TJTO): quando
    # a sessão restaurada é válida, o e-Proc dispara um alert() nativo
    # ("Usuário logado como fulano/... Utilize o botão 'Sair do
    # Sistema'...") ANTES de seguir com o redirecionamento esperado pro
    # painel. Como esse alert é bloqueante no JS da página e o handler
    # de diálogo (ver _log_dialog em abrir_sessao) o dispensa
    # automaticamente, a página fica parada nesse estado intermediário
    # -- NÃO necessariamente na tela final com todos os widgets JS já
    # inicializados (ex.: o plugin do multiselect de Classe Processual).
    # Sintoma real: todo termo de busca falhava esperando
    # "div.ms-parent.classeMultipleSelect" ficar visível, só quando a
    # sessão era restaurada (login novo não tinha esse problema).
    # Corrigido navegando pra URL de login MAIS UMA VEZ aqui -- com os
    # cookies já validados, essa segunda carga não deve mais disparar o
    # alert (ou se disparar, o handler dispensa de novo, mas agora
    # partindo de uma página já estável) e entrega a página final
    # limpa e totalmente inicializada pro resto do fluxo.
    page.goto(URL_LOGIN, timeout=NAVEGACAO_TIMEOUT_MS)
    verificar_e_resolver_captcha(page, onde="tela de login (2ª carga, sessão restaurada)")
    return True


def _login_keycloak(page):
    """
    Fluxo de login via Keycloak (SSO/OpenID Connect) -- confirmado no
    TJRS (keycloak-eks.tjrs.jus.br), com tokens dinâmicos (session_code,
    execution, state, nonce) que mudam a cada tentativa. O Playwright
    lida com os redirects e cookies automaticamente; só precisamos dos
    seletores dos formulários, confirmados via captura no Burp Suite:

      Formulário de usuário/senha (Keycloak):
        input#username, input#password, button#kc-login

      Formulário de código MFA (Keycloak, aparece após o login):
        input#otp, checkbox#saveDevice, button#kc-login
    """
    page.wait_for_selector("#username", timeout=TIMEOUT_PADRAO_MS)
    page.fill("#username", USUARIO)
    page.fill("#password", SENHA)
    page.click("#kc-login")

    verificar_e_resolver_captcha(page, onde="pós-login, antes do MFA")

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
    _verificar_mfa_aceito(page, onde="MFA Keycloak")


def _login_nativo(page):
    """
    Fluxo de login nativo do e-Proc (sem Keycloak/SSO) -- usado por
    tribunais que não roteiam o login por um SSO externo (confirmado no
    TJTO; outros tribunais e-Proc sem Keycloak tendem a compartilhar a
    mesma tela, por ser o mesmo software-base). Formulário na própria
    página do e-Proc:

      input#txtUsuario, input#pwdSenha, button#sbmEntrar

    IMPORTANTE: #pwdSenha é um campo "mascarado" -- existe também um
    <input type="password" name="pwdSenha" style="display:none"> oculto
    que parece ser sincronizado via JS a cada tecla digitada no campo
    visível (`type="text"`, classe `masked`). `page.fill()` seta o
    valor direto via DOM sem simular teclas de verdade, arriscando não
    disparar essa sincronização -- por isso usamos digitação simulada
    (`press_sequentially`) aqui.

    Confirmado também: o botão "Entrar" (#sbmEntrar) é
    `<button type="button" onclick="Submit('login')">`, não um botão
    de submit nativo -- um clique normal do Playwright já dispara esse
    onclick corretamente.

    MFA (TOTP) aparece na mesma página depois do clique, sem navegação:
        input#txtAcessoCodigo, button#btnValidar
    """
    page.wait_for_selector("#txtUsuario", timeout=TIMEOUT_PADRAO_MS)
    page.fill("#txtUsuario", USUARIO)
    page.locator("#pwdSenha").press_sequentially(SENHA, delay=50)
    page.click("#sbmEntrar")

    verificar_e_resolver_captcha(page, onde="pós-login, antes do MFA")

    # --- Etapa de MFA (TOTP) ---
    page.wait_for_selector("#txtAcessoCodigo", timeout=TIMEOUT_PADRAO_MS)
    codigo = gerar_codigo_mfa()
    page.fill("#txtAcessoCodigo", codigo)
    page.click("#btnValidar")

    page.wait_for_load_state("networkidle", timeout=NAVEGACAO_TIMEOUT_MS)
    _verificar_mfa_aceito(page, onde="MFA nativo")


def login(page):
    """
    Realiza o login no e-Proc, incluindo a etapa de MFA (TOTP).

    Detecta automaticamente qual fluxo de login usar (não depende de
    "qual tribunal é" -- só olha qual formulário está na tela): Keycloak/
    SSO (TJRS, `#username`) ou nativo do e-Proc (TJTO e possivelmente
    TJRO, `#txtUsuario`) -- ver _login_keycloak/_login_nativo. Isso
    evita ter que confirmar de antemão qual fluxo um tribunal novo usa;
    o script descobre sozinho na hora.
    """
    if ja_esta_logado(page):
        print("Sessão restaurada de execução anterior -- pulando login/MFA.")
        return

    # IMPORTANTE (confirmado em execução real no TJTO): checar por
    # `#txtUsuario` primeiro é um bug -- um tribunal pode servir tanto o
    # formulário nativo (então #txtUsuario existe e está VISÍVEL) quanto
    # uma página Keycloak (então #username está visível, mas o HTML
    # ainda inclui um #txtUsuario OCULTO residual/de relay do template
    # nativo). Checar #txtUsuario por EXISTÊNCIA (sem olhar visibilidade)
    # escolheria sempre o fluxo nativo errado nesse segundo caso.
    # #username é exclusivo do Keycloak -- checar ele primeiro resolve
    # os dois casos corretamente.
    # BUG CORRIGIDO: login() era o ÚNICO ponto do fluxo sem screenshot
    # em caso de erro (todo o resto -- consulta, listagem, detalhe --
    # já tira). Uma falha aqui (ex.: MFA que nunca aparece, timeout
    # inesperado num seletor) só deixava o traceback puro no log, sem
    # nenhuma evidência visual de ONDE a página ficou travada -- muito
    # mais difícil de diagnosticar que qualquer outra falha do fluxo.
    try:
        if page.query_selector("#username"):
            _login_keycloak(page)
        else:
            _login_nativo(page)
    except Exception:
        os.makedirs("output", exist_ok=True)
        page.screenshot(path="output/erro_login.png")
        print("Screenshot salvo em output/erro_login.png para diagnóstico.")
        raise


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
    verificar_e_resolver_captcha(page, onde="abrir consulta processual")


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
    verificar_e_resolver_captcha(page, onde="após consultar")


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

    # Sem isso, o Chromium headless se identifica com
    # "HeadlessChrome/..." no User-Agent -- confirmado em execução real
    # que pelo menos um tribunal (TJTO) bloqueia isso com 403 Forbidden
    # no nginx, antes de sequer chegar na aplicação (sintoma: qualquer
    # navegação trava em timeout esperando um elemento que nunca
    # existiu, porque a página inteira é a tela de erro do nginx, não
    # o e-Proc). O TJRS não bloqueia, mas usar um UA de navegador normal
    # não quebra nada lá também.
    user_agent = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    )

    if not forcar_novo_login and os.path.exists(STORAGE_STATE_PATH):
        context = browser.new_context(storage_state=STORAGE_STATE_PATH, user_agent=user_agent)
    else:
        context = browser.new_context(user_agent=user_agent)

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

    _verificar_cadastro_pendente(page)

    return browser, context, page


def _verificar_cadastro_pendente(page):
    """
    Algumas contas, ao logar, são redirecionadas para uma tela
    OBRIGATÓRIA de "Alterar Cadastro" (atualização cadastral -- CPF,
    RG, dados pessoais, etc.) antes de liberar o resto do sistema.
    Confirmado em execução real: nesse caso o menu "Consulta
    Processual" não existe na tela, e o crawler ficava travado 30s
    tentando clicar num link que não está lá, com uma mensagem de erro
    confusa (timeout genérico).

    Detectamos isso explicitamente para falhar rápido, com uma
    mensagem clara -- essa é uma ação que exige revisão humana
    (confirmar/corrigir dados cadastrais pessoais), não algo que o
    crawler deve preencher e salvar sozinho.
    """
    titulo = page.query_selector("h1, h2, h3")
    if titulo and "Alterar Cadastro" in titulo.inner_text():
        raise RuntimeError(
            "A conta caiu numa tela OBRIGATÓRIA de 'Alterar Cadastro' "
            "(atualização cadastral) logo após o login -- o e-Proc exige "
            "que os dados pessoais sejam revisados/confirmados manualmente "
            "antes de liberar o resto do sistema. Entre nessa conta pelo "
            "navegador comum, complete o cadastro, e rode o crawler de novo."
        )


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