"""
Crawler de varredura em massa do e-Proc: busca genérica por nome de
parte, percorre todas as empresas encontradas (com paginação), e para
cada uma abre a lista de processos dela.

A tabela de resultados (`#divInfraAreaTabela`) é renderizada pela
biblioteca DataTables (confirmado pelas classes `dataTable`/`no-footer`
e pelo padrão de IDs auto-gerados: `{id}_info`, `{id}_paginate`,
`{id}_next` etc. são convenção fixa dessa biblioteca) -- por isso a
paginação abaixo pode ser escrita com bastante confiança, mesmo sem ver
o HTML exato do rodapé de paginação.
"""

import json
import os
import re
import time
from decimal import Decimal, InvalidOperation
from urllib.parse import urljoin

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from eproc_automation import selecionar_classe_processual, consultar, URL_LOGIN


def aguardar_entre_requisicoes():
    """
    Pausa configurável antes de cada ação que bate no servidor do
    e-Proc (clicar em Consultar, paginar, abrir empresa, abrir
    processo) -- evita sobrecarregar o site. Configurável via
    CRAWLER_DELAY_ENTRE_REQUISICOES_SEGUNDOS no .env (padrão: 5s).
    """
    segundos = float(os.environ.get("CRAWLER_DELAY_ENTRE_REQUISICOES_SEGUNDOS", "5"))
    if segundos > 0:
        time.sleep(segundos)


def maximizar_resultados_por_pagina(page, table_id: str, valor: str = "100"):
    """
    Seleciona a maior opção de "resultados por página" disponível no
    dropdown padrão do DataTables (`#{table_id}_length`, opções
    10/25/50/100 confirmadas no HTML real). Reduz drasticamente o
    número de cliques de paginação necessários -- ex.: uma busca com
    2.141 resultados precisaria de 215 páginas em 10-por-página, mas
    só ~22 em 100-por-página.
    """
    select_length = page.query_selector(f"select[name='{table_id}_length']")
    if not select_length:
        return  # tabela pequena, sem esse controle renderizado

    page.select_option(f"select[name='{table_id}_length']", valor)
    try:
        page.wait_for_load_state("networkidle", timeout=15_000)
    except PlaywrightTimeoutError:
        pass
    page.wait_for_timeout(400)


def paginar_datatable(page, table_id: str, extrator_linha, timeout_ms: int = 30_000, ao_encontrar_item=None):
    """
    Percorre todas as páginas de uma tabela DataTables, chamando
    extrator_linha(row_element) para cada <tr> do <tbody> e devolvendo
    a lista de itens não-None retornados.

    extrator_linha deve retornar um dict com os dados da linha, ou
    None para pular linhas irrelevantes (ex.: "nenhum resultado").

    ao_encontrar_item(item), se fornecido, é chamado IMEDIATAMENTE para
    cada item extraído, antes de seguir para a próxima linha/página --
    importante para casos onde o item tem um link/hash de sessão que
    pode expirar se só for visitado muito depois (ver uso em
    buscar_empresas_por_termo, que processa cada empresa assim que
    encontrada, em vez de esperar a paginação inteira das milhares de
    empresas terminar antes de começar a visitá-las).
    """
    resultados = []
    pagina = 1
    MAX_PAGINAS = 500  # proteção contra loop infinito (ex.: se o botão
                        # "next" nunca ficar "disabled" por algum motivo)

    while True:
        linhas = page.query_selector_all(f"#{table_id} tbody tr")
        for linha in linhas:
            item = extrator_linha(linha)
            if item is not None:
                resultados.append(item)
                if ao_encontrar_item:
                    ao_encontrar_item(item)

        print(f"    página {pagina}: {len(resultados)} item(ns) coletado(s) até agora")

        if pagina >= MAX_PAGINAS:
            print(f"    ⚠ Atingiu o limite de {MAX_PAGINAS} páginas -- parando por segurança.")
            break

        next_button = page.query_selector(f"#{table_id}_next")
        if not next_button:
            break  # tabela pequena, sem paginação renderizada

        classes = next_button.get_attribute("class") or ""
        if "disabled" in classes:
            break  # já está na última página

        aguardar_entre_requisicoes()
        next_button.click()
        pagina += 1
        try:
            page.wait_for_load_state("networkidle", timeout=timeout_ms)
        except PlaywrightTimeoutError:
            pass  # alguns temas de DataTables não disparam eventos de rede visíveis
        page.wait_for_timeout(300)  # pequena folga para o redraw da tabela

    return resultados


def extrair_linha_empresa(linha) -> dict | None:
    """
    Extrai id_pessoa, nome e cpf_cnpj de uma linha da tabela de partes
    (confirmado via HTML real que você mandou):

        <tr data-idpessoa="..." data-partenome="...">
            <td><a class="pesquisaNomeParteBuscarProcessos" href="...">NOME</a></td>
            <td>CPF/CNPJ</td>
        </tr>
    """
    id_pessoa = linha.get_attribute("data-idpessoa")
    if not id_pessoa:
        return None

    nome = linha.get_attribute("data-partenome") or ""
    celulas = linha.query_selector_all("td")
    cpf_cnpj = celulas[1].inner_text().strip() if len(celulas) > 1 else None

    link = linha.query_selector("a.pesquisaNomeParteBuscarProcessos")
    href = link.get_attribute("href") if link else None

    return {
        "id_pessoa": id_pessoa,
        "nome": nome,
        "cpf_cnpj": cpf_cnpj,
        "href": href,
    }


def _procurar_mensagem_erro_inline(page) -> str | None:
    """
    Procura por mensagens de erro/aviso mostradas na própria página
    (não em popup JS) após uma submissão que não deu o resultado
    esperado. Tenta seletores comuns em frameworks judiciários desse
    estilo ("infra*") e genéricos (.alert, [role=alert]).
    """
    seletores_candidatos = [
        "#divInfraMensagem",
        ".infraAviso",
        ".infraMensagemErro",
        "[role='alert']",
        ".alert",
        ".alert-danger",
        ".swal2-html-container",  # caso use SweetAlert em vez de alert() nativo
    ]
    for seletor in seletores_candidatos:
        el = page.query_selector(seletor)
        if el:
            texto = el.inner_text().strip()
            if texto:
                return texto
    return None


def buscar_empresas_por_termo(page, termo: str, ao_encontrar_empresa=None) -> list[dict]:
    """
    Preenche o campo de busca 'Nome da Parte' com um termo genérico
    (ex.: "LTDA"), marca as classes processuais configuradas e clica
    em 'Consultar'. Retorna a lista de empresas encontradas, já
    maximizando resultados por página e paginando por todas as
    páginas de resultado.

    CORREÇÃO IMPORTANTE (confirmada via screenshot em execução real):
    a suposição original de que a tabela de resultados apareceria
    automaticamente via autocomplete conforme o usuário digita estava
    ERRADA. Digitar sozinho não muda a tela -- é preciso marcar a
    Classe Processual e clicar em "Consultar" (igual ao fluxo de
    consulta única) para a tabela ser de fato renderizada. O payload
    de autocomplete que víamos em capturas de tráfego era, na
    verdade, a resposta da própria submissão do formulário, não uma
    busca incremental por tecla digitada.

    Ainda assim usamos page.type() (digitação caractere a caractere)
    em vez de fill() para o campo de nome, por segurança -- não
    prejudica em nada mesmo que não seja estritamente necessário aqui.

    ao_encontrar_empresa(empresa), se fornecido, processa cada empresa
    IMEDIATAMENTE ao ser encontrada na listagem (em vez de só depois
    que TODAS as páginas de resultado forem percorridas). Isso resolve
    um bug real observado em produção: o href/hash de cada empresa é
    um token de sessão que pode expirar/invalidar por navegação -- se
    esperarmos processar centenas de outras empresas antes de visitar
    uma específica, o hash dela pode não funcionar mais (redireciona
    para o "Painel do Advogado" em vez da lista de processos).
    Processando cada empresa assim que encontrada, a janela entre
    "encontrar" e "visitar" cai de "potencialmente horas" para
    "segundos".
    """
    page.wait_for_selector("input[name='strNomeParte']", timeout=30_000)
    page.fill("input[name='strNomeParte']", "")  # limpa antes de digitar
    page.type("input[name='strNomeParte']", termo, delay=100)

    selecionar_classe_processual(page)
    aguardar_entre_requisicoes()
    consultar(page)

    try:
        page.wait_for_selector("#divInfraAreaTabela tbody tr", timeout=30_000)
    except PlaywrightTimeoutError:
        page.screenshot(path="output/erro_busca_nome_parte.png")
        mensagem_erro = _procurar_mensagem_erro_inline(page)
        detalhe = f" Mensagem encontrada na página: {mensagem_erro!r}" if mensagem_erro else (
            " Nenhuma mensagem de erro inline encontrada -- pode ter sido "
            "um alert()/confirm() JS (ver log '[DIÁLOGO JS]' acima, se houver)."
        )
        raise PlaywrightTimeoutError(
            f"Tabela de resultados não apareceu após consultar '{termo}' em "
            f"Nome da Parte. Screenshot salvo em output/erro_busca_nome_parte.png."
            f"{detalhe}"
        )

    page.wait_for_timeout(500)  # pequena folga para a tabela assentar

    info = page.query_selector("#divInfraAreaTabela_info")
    if info:
        print(f"  {info.inner_text().strip()}")

    print("  Ajustando para 100 resultados por página...")
    maximizar_resultados_por_pagina(page, "divInfraAreaTabela")
    print("  Iniciando paginação...")

    return paginar_datatable(
        page, "divInfraAreaTabela", extrair_linha_empresa,
        ao_encontrar_item=ao_encontrar_empresa,
    )


def abrir_processos_da_empresa(page, empresa: dict, referer: str = None):
    """
    Navega até a lista de processos da empresa usando o href capturado
    antes, mais confiável que re-clicar via seletor genérico que pode
    casar com a linha errada após reordenação da tabela.

    IMPORTANTE: o href vem RELATIVO (ex.: "controlador.php?acao=...").
    Isso funciona normalmente num <a href> clicado pelo usuário (o
    navegador resolve sozinho contra a URL atual), mas page.goto() do
    Playwright exige uma URL absoluta -- por isso resolvemos com
    urljoin() antes de navegar.

    BUG CORRIGIDO (2ª rodada): a primeira correção usou URL_LOGIN puro
    (só o domínio, ex.: "https://eproc1g.tjrs.jus.br") como base do
    urljoin -- mas o sistema roda sob o caminho "/eproc/"
    (ex.: ".../eproc/controlador.php?..."). Sem esse segmento na base,
    urljoin() gerava uma URL válida só na aparência, mas apontando pro
    lugar errado -- o servidor respondia "File not found." (confirmado
    via screenshot em execução real). Corrigido incluindo "/eproc/" na
    URL base antes de resolver o link relativo.

    BUG CORRIGIDO (3ª rodada): mesmo com a URL certa, 100% das
    empresas passaram a cair no "Painel do Advogado" -- não como algo
    intermitente (hash expirado por tempo), mas sistematicamente, já
    na primeira empresa de cada busca. Suspeita forte: o e-Proc exige
    um cabeçalho HTTP Referer válido (a página de onde o link "veio")
    para aceitar a navegação -- proteção comum contra acesso direto a
    links profundos. page.goto() do Playwright NÃO envia Referer por
    padrão (diferente de um clique real, que sempre inclui), e como
    passamos a navegar numa aba nova (sem histórico), toda navegação
    saía sem esse cabeçalho. Corrigido passando `referer` explicitamente
    (a URL da página de busca principal), simulando que o link foi
    clicado a partir de lá.
    """
    if empresa.get("href"):
        base_eproc = URL_LOGIN.rstrip("/") + "/eproc/"
        url_absoluta = urljoin(base_eproc, empresa["href"])
        aguardar_entre_requisicoes()
        if referer:
            page.goto(url_absoluta, timeout=60_000, referer=referer)
        else:
            page.goto(url_absoluta, timeout=60_000)
    else:
        page.click(f"tr[data-idpessoa='{empresa['id_pessoa']}'] a")
    page.wait_for_load_state("networkidle", timeout=60_000)

    _recuperar_se_caiu_no_formulario_sem_classe(page)


def _recuperar_se_caiu_no_formulario_sem_classe(page):
    """
    Quando o link direto da empresa (num_id_parte + hash) falha, o
    e-Proc não mostra uma tela de erro genérica -- ele volta para o
    FORMULÁRIO de Consulta Processual, já com "Nome Parte" preenchido
    corretamente (extraído do parâmetro str_nome_parte da própria URL
    que falhou), mas SEM nenhuma Classe Processual marcada. Sem esse
    filtro, a busca automática já-executada mostra "Nenhum processo
    (em movimento) encontrado." -- não porque não existam processos,
    mas porque falta o filtro de classe (confirmado via screenshot em
    execução real).

    Como o nome já está certo, dá pra RECUPERAR ali mesmo: só falta
    marcar a Classe Processual de novo e clicar Consultar -- sem
    precisar esperar o próximo ciclo completo do crawler.

    IMPORTANTE: o campo "Nome Parte" existe no MESMO template de
    página tanto no caso de sucesso quanto de falha (é o mesmo
    formulário por cima dos resultados) -- por isso a detecção exige
    TAMBÉM a mensagem exata "Nenhum processo (em movimento)
    encontrado.", não só a presença do campo, evitando disparar a
    recuperação numa página que na verdade já carregou com sucesso.
    """
    campo_nome_parte = page.query_selector("input[name='strNomeParte']")
    if not campo_nome_parte:
        return  # não é essa tela -- nada a recuperar aqui

    valor_nome = campo_nome_parte.input_value()
    if not valor_nome:
        return  # formulário vazio -- não é o caso de recuperação

    mensagem_nao_encontrado = page.get_by_text("Nenhum processo (em movimento) encontrado")
    if mensagem_nao_encontrado.count() == 0:
        return  # tem nome preenchido, mas não é o caso de falha -- provavelmente já carregou certo

    print("    ↻ Caiu no formulário sem Classe Processual selecionada -- recuperando...")
    selecionar_classe_processual(page)
    aguardar_entre_requisicoes()
    consultar(page)

    # IMPORTANTE: consultar() já espera "networkidle", mas isso nem
    # sempre é suficiente -- a busca desta página parece ser via AJAX,
    # e o "networkidle" pode considerar a página "pronta" antes da
    # resposta real terminar de renderizar (confirmado em execução
    # real: screenshot capturado mostrando "Carregando..." ainda
    # visível). Por isso esperamos explicitamente pelo resultado de
    # verdade -- tabela de processos OU mensagem de "sem resultado" --
    # antes de devolver o controle para quem chamou esta função.
    try:
        linhas = page.locator("#divInfraAreaTabela tbody tr")
        sem_resultado = page.get_by_text("Nenhum processo").or_(page.get_by_text("Nenhum registro"))
        linhas.or_(sem_resultado).first.wait_for(timeout=20_000)
    except PlaywrightTimeoutError:
        print("    ⚠ Recuperação: resultado ainda não apareceu após 20s -- seguindo mesmo assim.")


# ---------------------------------------------------------------------
# Lista de processos de uma empresa + detalhe de um processo
# ---------------------------------------------------------------------

def extrair_linha_processo(linha) -> dict | None:
    """
    Extrai os dados de uma linha da lista de processos de uma empresa
    (confirmado via HTML real: mesma tabela #divInfraAreaTabela, agora
    com colunas de processo em vez de partes):

        Nº Processo | Data de Autuação | Juízo | Autor | Réu |
        Classe Judicial | Último Evento | Assunto(s) | Situação

    O link do número do processo abre em nova aba (target="_blank"),
    mas como é uma URL normal, navegamos direto via page.goto() no
    lugar de lidar com popup -- muito mais simples e confiável.
    """
    link = linha.query_selector("td.sorting_1 a")
    if not link:
        return None

    celulas = linha.query_selector_all("td")

    def texto(indice):
        return celulas[indice].inner_text().strip() if len(celulas) > indice else None

    return {
        "numero_processo": link.inner_text().strip(),
        "href": link.get_attribute("href"),
        "data_autuacao": texto(1),
        "juizo": texto(2),
        "autor": texto(3),
        "reu": texto(4),
        "classe_processual": texto(5),
        "ultimo_evento": texto(6),
        "assuntos": texto(7),
        "situacao": texto(8),
    }


def listar_processos_da_empresa(page) -> list[dict]:
    """
    Lista todos os processos de uma empresa (já na tela após
    abrir_processos_da_empresa()), com paginação e resultados por
    página maximizados. Autor, réu, data de autuação e classe já vêm
    preenchidos direto da listagem -- só "valor da causa" e "OAB" só
    aparecem no detalhe de cada processo (ver extrair_detalhe_processo).

    IMPORTANTE (corrigido após observação em produção): a maioria das
    empresas encontradas por um termo de busca genérico NÃO tem
    nenhum processo nas Classes Processuais selecionadas -- o e-Proc
    mostra a mensagem "Nenhum processo (em movimento) encontrado."
    nesse caso, SEM renderizar a tabela (#divInfraAreaTabela tbody tr
    nunca aparece). Isso é um resultado válido, não um erro -- por
    isso detectamos essa mensagem explicitamente, em vez de esperar o
    timeout inteiro (30s) e tratar como falha a cada vez. Sem essa
    distinção, cada uma dessas (muito comuns) "sem resultado" custava
    30s de espera à toa E fazia a empresa nunca ser marcada como
    processada no banco (o erro interrompia antes de chegar em
    marcar_empresa_processada), sendo retestada para sempre em todo
    ciclo futuro.
    """
    linhas_locator = page.locator("#divInfraAreaTabela tbody tr")
    sem_resultado_locator = page.get_by_text("Nenhum processo").or_(
        page.get_by_text("Nenhum registro")
    )
    painel_advogado_locator = page.get_by_text("Painel do Advogado")
    combinado = linhas_locator.or_(sem_resultado_locator).or_(painel_advogado_locator)

    try:
        combinado.first.wait_for(timeout=15_000)
    except PlaywrightTimeoutError:
        page.screenshot(path="output/erro_listar_processos.png")
        mensagem_erro = _procurar_mensagem_erro_inline(page)
        detalhe = (
            f" Mensagem encontrada na página: {mensagem_erro!r}" if mensagem_erro
            else " Nenhuma mensagem de erro/resultado encontrada -- pode ser bloqueio "
                 "por 'muitos processos' (ver log '[DIÁLOGO JS]' acima, se houver)."
        )
        raise PlaywrightTimeoutError(
            f"Nem tabela nem mensagem de 'sem resultado' apareceram para esta "
            f"empresa. Screenshot salvo em output/erro_listar_processos.png.{detalhe}"
        )

    if painel_advogado_locator.count() > 0 and linhas_locator.count() == 0:
        # Caso identificado em produção: em vez de mostrar a lista de
        # processos (ou "nenhum encontrado"), o e-Proc às vezes
        # redireciona para a tela inicial ("Painel do Advogado"),
        # provavelmente porque o hash/link da empresa (token de
        # sessão) expirou entre o momento em que a empresa foi listada
        # e o momento em que foi visitada -- comum em varreduras
        # longas com muitas empresas em fila. Diferente do "sem
        # resultado" (que é um resultado válido), aqui a informação
        # pode ter sido perdida -- por isso levantamos um erro
        # distinto e claro, em vez de mascarar como sucesso silencioso.
        page.screenshot(path="output/erro_painel_advogado.png")
        raise PlaywrightTimeoutError(
            "Redirecionado para o 'Painel do Advogado' em vez da lista de "
            "processos -- provável expiração do hash/link da empresa (token "
            "de sessão). Screenshot salvo em output/erro_painel_advogado.png. "
            "Esta empresa pode precisar ser revisitada depois."
        )

    if linhas_locator.count() == 0:
        # Não há linhas de resultado real -- foi a mensagem de "nenhum
        # processo encontrado" que apareceu. Resultado válido, não é erro.
        print("    Nenhum processo encontrado para esta empresa nas classes selecionadas.")
        return []

    maximizar_resultados_por_pagina(page, "divInfraAreaTabela")

    processos = paginar_datatable(page, "divInfraAreaTabela", extrair_linha_processo)

    base_url = page.url
    for processo in processos:
        if processo.get("href"):
            processo["url"] = urljoin(base_url, processo["href"])

    return processos


def _expandir_informacoes_adicionais(page):
    """
    Confirmado via HTML real: a seção "Informações Adicionais" é
    controlada por um <legend id="legInfAdicional"> clicável (não o
    <fieldset> pai, não um <a> -- os seletores anteriores erravam o
    alvo exato), com:

        onclick="carregarInformacoesAdicionais('controlador.php?
                  acao=processo_montar_informacoes_adicionais&...')"

    IMPORTANTE: esse clique dispara uma requisição AJAX que BUSCA e
    POPULA o conteúdo dinamicamente (lazy load) -- não é um simples
    toggle de CSS. Por isso esperamos o conteúdo real aparecer via
    wait_for_function, em vez de confiar num tempo fixo de espera.
    """
    try:
        conteudo = page.query_selector("#fldInformacoesAdicionais_content")
        # Se já tem algum <span> com texto dentro, já está carregado
        # (pode acontecer se "Manter Informações Adicionais Abertas"
        # estiver marcado na conta usada para o login).
        if conteudo and conteudo.query_selector("span"):
            return

        cabecalho = page.query_selector("#legInfAdicional")
        if not cabecalho:
            print("    ⚠ Cabeçalho #legInfAdicional não encontrado nesta página.")
            return

        cabecalho.click()

        # Espera o AJAX de carregarInformacoesAdicionais popular o
        # conteúdo de verdade antes de seguir.
        page.wait_for_function(
            """() => {
                const el = document.querySelector('#fldInformacoesAdicionais_content');
                return el && el.querySelector('span') && el.innerText.trim().length > 0;
            }""",
            timeout=10_000,
        )
    except Exception as e:
        print(f"    ⚠ Erro ao tentar expandir 'Informações Adicionais': {e}")


def _valor_por_rotulo_informacoes_adicionais(page, rotulo: str) -> str | None:
    """
    Busca um valor dentro da seção "Informações Adicionais" pelo texto
    do rótulo (ex.: "Valor da Causa"). Estrutura real confirmada:

        <div class="row">
            <span>Valor da Causa:</span>
            <span class="... font-weight-bold"> R$ 13.816,38 </span>
        </div>

    Usa busca por texto em vez de ID porque vários desses blocos
    reaproveitam o mesmo id (ex.: "info-vista-proc" aparece 2x na
    página), tornando IDs não confiáveis aqui.
    """
    container = page.query_selector("#fldInformacoesAdicionais_content")
    if not container:
        return None

    linhas = container.query_selector_all("div.row")
    for linha in linhas:
        spans = linha.query_selector_all(":scope > span")
        if len(spans) >= 2 and rotulo in spans[0].inner_text():
            return spans[1].inner_text().strip()
    return None


def _extrair_oab(page) -> str | None:
    """
    Extrai o número da OAB (formato "UF" + 6 dígitos, ex.: RS051857 --
    confirmado no popover do próprio formulário de busca por OAB) do
    bloco "Partes e Representantes", coluna do Autor.
    """
    autor_td = page.query_selector("#tblPartesERepresentantes td.autorReu")
    if not autor_td:
        return None
    texto = autor_td.inner_text()
    match = re.search(r"\b([A-Z]{2}\d{6})\b", texto)
    return match.group(1) if match else None


def _extrair_cnpj_partes(page) -> dict:
    """
    Extrai o CNPJ do autor e do réu da tabela "Partes e
    Representantes", procurando pelo padrão XX.XXX.XXX/XXXX-XX que
    aparece entre parênteses ao lado do nome da parte (confirmado via
    print real do usuário). Assume a mesma ordem de colunas usada em
    _extrair_oab: primeiro td.autorReu = Autor, segundo = Réu.
    """
    celulas = page.query_selector_all("#tblPartesERepresentantes td.autorReu")
    padrao_cnpj = re.compile(r"(\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2})")

    def extrair(indice):
        if len(celulas) <= indice:
            return None
        texto = celulas[indice].inner_text()
        match = padrao_cnpj.search(texto)
        return match.group(1) if match else None

    return {
        "cnpj_autor": extrair(0),
        "cnpj_reu": extrair(1),
    }


def _autor_em_recuperacao_judicial(page) -> bool:
    """
    Detecta se especificamente o AUTOR está marcado como "EM
    RECUPERAÇÃO JUDICIAL" no bloco "Partes e Representantes" da
    página de detalhe -- confirmado via print real do usuário.

    IMPORTANTE: a checagem é só do lado do AUTOR, não do réu. Réu em
    recuperação judicial é mantido normalmente (pode ser justamente um
    alvo relevante -- credor precisando de representação no processo
    de recuperação). Só quando o próprio autor da ação está em
    recuperação judicial é que o processo é descartado.

    Usa o mesmo seletor de _extrair_oab (primeiro `td.autorReu` no
    DOM, que corresponde à coluna do Autor -- ela vem antes da coluna
    do Réu na tabela).
    """
    autor_td = page.query_selector("#tblPartesERepresentantes td.autorReu")
    if not autor_td:
        return False
    texto = autor_td.inner_text().upper()
    return "RECUPERAÇÃO JUDICIAL" in texto or "RECUPERACAO JUDICIAL" in texto


def extrair_detalhe_processo(page) -> dict:
    """
    Extrai os dados que só existem na página de detalhe do processo
    (não disponíveis na listagem): valor da causa e OAB do advogado.
    Também recolhe numero_processo/classe/data como conferência extra,
    usando os seletores reais confirmados via HTML:

        #txtNumProcesso, #txtClasse, #txtAutuacao
    """
    _expandir_informacoes_adicionais(page)

    def texto_de(seletor):
        el = page.query_selector(seletor)
        return el.inner_text().strip() if el else None

    return {
        "numero_processo": texto_de("#txtNumProcesso"),
        "classe_processual": texto_de("#txtClasse"),
        "data_autuacao": texto_de("#txtAutuacao"),
        "valor_causa": _valor_por_rotulo_informacoes_adicionais(page, "Valor da Causa"),
        "oab": _extrair_oab(page),
        "autor_em_recuperacao_judicial": _autor_em_recuperacao_judicial(page),
        **_extrair_cnpj_partes(page),
        "url": page.url,
    }


# ---------------------------------------------------------------------
# Filtros
# ---------------------------------------------------------------------

def _parse_valor_brl(texto: str) -> Decimal | None:
    """Converte 'R$ 12.345,67' -> Decimal('12345.67')."""
    if not texto:
        return None
    limpo = re.sub(r"[^\d,]", "", texto).replace(",", ".")
    try:
        return Decimal(limpo)
    except InvalidOperation:
        return None


def _parse_data_br(texto: str):
    """
    Converte 'dd/mm/aaaa' ou 'dd/mm/aaaa hh:mm:ss' -> datetime.date, ou
    None se inválido. A listagem de processos traz a data com horário
    (ex.: "09/01/2017 00:00:00"), por isso pegamos só a parte da data.
    """
    from datetime import datetime as dt
    if not texto:
        return None
    data_str = texto.strip().split(" ")[0]
    try:
        return dt.strptime(data_str, "%d/%m/%Y").date()
    except ValueError:
        return None


def processo_bate_filtro_preliminar(dados: dict, config: dict) -> bool:
    """
    Filtro rápido usando só os dados já disponíveis na LISTAGEM (autor,
    réu, data de autuação) -- evita a navegação e o carregamento da
    página de detalhe (que só é necessária para valor da causa e OAB)
    para processos que já claramente não servem. Importante para a
    escala do crawler: evita centenas de navegações desnecessárias.
    """
    autor = (dados.get("autor") or "").upper()
    if not any(sufixo.upper() in autor for sufixo in config["autor_sufixos"]):
        return False

    # BUG CORRIGIDO: o autor também não pode ser um dos tipos a ignorar
    # (banco, cooperativa, etc.) -- antes só checávamos isso no réu, e
    # nomes como "COOPERATIVA ... LTDA" passavam pelo filtro de autor
    # (contém "LTDA") sem essa checagem adicional. Confirmado em
    # produção: processo real salvo com autor
    # "COOPERATIVA DE ECONOMIA E CREDITO MUTUO UNICRED INTEGRACAO LTDA".
    if any(termo.upper() in autor for termo in config["reu_ignorar"]):
        return False

    reu = (dados.get("reu") or "").upper()
    if any(termo.upper() in reu for termo in config["reu_ignorar"]):
        return False

    # Checagem preliminar e barata: se "recuperação judicial" já
    # aparecer no próprio texto do AUTOR na listagem, rejeita antes de
    # gastar uma navegação. IMPORTANTE: só do lado do autor -- réu em
    # recuperação judicial é mantido normalmente (pode ser justamente
    # um alvo relevante). A checagem definitiva (via tabela "Partes e
    # Representantes" da página de detalhe) acontece depois, em
    # processo_bate_com_filtros -- essa aqui é só uma otimização.
    if "RECUPERAÇÃO JUDICIAL" in autor or "RECUPERACAO JUDICIAL" in autor:
        return False

    data_autuacao = _parse_data_br(dados.get("data_autuacao"))
    if data_autuacao and not (config["data_inicio"] <= data_autuacao <= config["data_fim"]):
        return False

    return True


def processo_bate_com_filtros(dados: dict, config: dict) -> bool:
    """
    Aplica os critérios descritos:
        - autor contém um dos sufixos configurados (LTDA, EIRELI, ...)
        - autor NÃO é um dos tipos a ignorar (banco, cooperativa, ...)
        - réu NÃO contém nenhum dos termos a ignorar (banco, etc.)
        - autor NÃO está "EM RECUPERAÇÃO JUDICIAL" (réu pode estar --
          é mantido normalmente, pode ser justamente um alvo relevante)
        - data de autuação dentro do intervalo
        - valor da causa dentro do intervalo
        - OAB do advogado na UF configurada
    """
    autor = (dados.get("autor") or "").upper()
    if not any(sufixo.upper() in autor for sufixo in config["autor_sufixos"]):
        return False

    if any(termo.upper() in autor for termo in config["reu_ignorar"]):
        return False

    reu = (dados.get("reu") or "").upper()
    if any(termo.upper() in reu for termo in config["reu_ignorar"]):
        return False

    # Checagem definitiva (dado real extraído da página de detalhe,
    # tabela "Partes e Representantes" -- ver
    # _autor_em_recuperacao_judicial em extrair_detalhe_processo).
    # Só rejeita se for o AUTOR em recuperação judicial -- réu nessa
    # situação é mantido de propósito (ver conversa com o usuário).
    if dados.get("autor_em_recuperacao_judicial"):
        return False

    data_autuacao = _parse_data_br(dados.get("data_autuacao"))
    if data_autuacao:
        if not (config["data_inicio"] <= data_autuacao <= config["data_fim"]):
            return False

    valor = _parse_valor_brl(dados.get("valor_causa"))
    if valor is not None:
        if not (config["valor_min"] <= valor <= config["valor_max"]):
            return False

    oab_texto = (dados.get("oab") or "").upper()
    if config["oab_uf"] and config["oab_uf"].upper() not in oab_texto:
        return False

    return True