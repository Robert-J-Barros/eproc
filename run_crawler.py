"""
Crawler de varredura em massa do e-Proc.

Fluxo:
    1. Login (reaproveita eproc_automation.abrir_sessao)
    2. Para cada termo de busca configurado (CRAWLER_TERMOS_BUSCA):
       a. Abre Consulta Processual, seleciona Tipo de Pesquisa = Nome
          da Parte, digita o termo genérico (ex.: "LTDA")
       b. Pagina pela lista de empresas encontradas (DataTables)
       c. Para cada empresa NÃO processada ainda (dedupe via MySQL):
          - Abre a lista de processos dela
          - Pagina pela lista de processos
          - Para cada processo: abre o detalhe, extrai os dados,
            aplica os filtros configurados, salva no MySQL se bater
    3. Ao terminar todos os termos, dorme por CRAWLER_INTERVALO_CICLO_SEGUNDOS
       e repete o ciclo (loop contínuo -- útil para pegar processos
       novos que forem sendo distribuídos com o tempo)

Checkpoint: salvo no MySQL a cada empresa processada, permitindo
retomar de onde parou caso o container seja reiniciado no meio de um
ciclo.

Uso:
    python run_crawler.py
"""

import os
import time
from datetime import date

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

from eproc_automation import (
    abrir_sessao, abrir_consulta_processual,
    selecionar_tipo_pesquisa_nome_da_parte, validar_configuracao,
    TIMEOUT_PADRAO_MS,
)
from db import Database
from crawler import (
    buscar_empresas_por_termo, abrir_processos_da_empresa,
    listar_processos_da_empresa, extrair_detalhe_processo,
    processo_bate_filtro_preliminar, processo_bate_com_filtros,
    _parse_valor_brl, aguardar_entre_requisicoes,
)


def _lista_env(nome: str, padrao: str) -> list:
    valor = os.environ.get(nome, padrao)
    return [item.strip() for item in valor.split(",") if item.strip()]


def _data_env(nome: str, padrao: str) -> date:
    valor = os.environ.get(nome, padrao)
    ano, mes, dia = map(int, valor.split("-"))
    return date(ano, mes, dia)


def _gerar_termos_busca(config_sufixos: list) -> list:
    """
    O e-Proc exige pelo menos DUAS "partículas" (palavras separadas por
    espaço) no campo Nome da Parte -- confirmado em execução real via
    alert() nativo do navegador: "Informe um nome para consulta com
    pelo menos duas partículas." Um termo isolado como "LTDA" é sempre
    rejeitado pelo site.

    ESTRATÉGIA PRINCIPAL (CRAWLER_TERMOS_BUSCA_EXTRA): divide UMA
    palavra ampla em duas metades com espaço no meio (ex.: "COMERCIO"
    -> "Comer cio"), satisfazendo a validação de 2 partículas enquanto
    ainda busca efetivamente o termo único -- qualquer parte com
    "COMERCIO" no nome continua batendo, já que as duas metades
    aparecem juntas nesse trecho. Validado pelo usuário: "Comer cio" e
    "Indus tria" dão os maiores volumes de resultado.

    ESTRATÉGIA ALTERNATIVA (CRAWLER_PALAVRAS_BASE, opcional): combina
    cada palavra com os sufixos societários (CRAWLER_AUTOR_SUFIXOS) --
    produto cartesiano. Ex.: palavras=[SERVICOS] + sufixos=[LTDA] ->
    ["SERVICOS LTDA"]. Deixe CRAWLER_PALAVRAS_BASE vazio para não usar.
    """
    palavras_base = _lista_env("CRAWLER_PALAVRAS_BASE", "")
    gerados = [f"{palavra} {sufixo}" for palavra in palavras_base for sufixo in config_sufixos]
    extras = _lista_env("CRAWLER_TERMOS_BUSCA_EXTRA", "")
    # Remove duplicatas preservando ordem
    return list(dict.fromkeys(gerados + extras))


def carregar_configuracao_filtros() -> dict:
    from decimal import Decimal
    autor_sufixos = _lista_env("CRAWLER_AUTOR_SUFIXOS", "LTDA,EIRELI")
    return {
        "termos_busca": _gerar_termos_busca(autor_sufixos),
        "autor_sufixos": autor_sufixos,
        "reu_ignorar": _lista_env(
            "CRAWLER_REU_IGNORAR", "BANCO,COOPERATIVA,MUNICIPIO,ESTADO"
        ),
        "data_inicio": _data_env("CRAWLER_DATA_INICIO", "2018-01-01"),
        "data_fim": _data_env("CRAWLER_DATA_FIM", "2025-08-30"),
        "valor_min": Decimal(os.environ.get("CRAWLER_VALOR_MIN", "5000.00")),
        "valor_max": Decimal(os.environ.get("CRAWLER_VALOR_MAX", "350000.00")),
        "oab_uf": os.environ.get("CRAWLER_OAB_UF", "RS"),
    }


def processar_empresa(pagina_empresa, db: Database, empresa: dict, config: dict, referer: str = None):
    """
    Abre os processos de uma empresa e filtra cada um em duas etapas:

    1. Filtro preliminar (autor/réu/data) usando só os dados já
       disponíveis na LISTAGEM -- sem navegar para lugar nenhum.
    2. Só para os que passam na etapa 1, abre o detalhe do processo
       (necessário para valor da causa e OAB) e aplica o filtro
       completo antes de salvar.

    Isso evita abrir centenas de páginas de detalhe desnecessárias.

    IMPORTANTE: `pagina_empresa` é uma ABA PRÓPRIA (nova), separada da
    aba principal usada para paginar a lista de empresas -- ver
    executar_ciclo(). Isso existe para resolver um bug real observado
    em produção: o href/hash de cada empresa é um token de sessão que
    pode expirar se a gente demorar demais (por navegação acumulada)
    entre encontrar a empresa e visitá-la. Processando numa aba nova,
    imediatamente ao encontrar, e sem nunca navegar a aba principal de
    busca para longe da sua posição de paginação, eliminamos essa
    janela de expiração quase por completo.

    `referer` -- a URL da página de busca principal, passada adiante
    para simular que o link foi clicado de lá (ver bug do cabeçalho
    Referer documentado em abrir_processos_da_empresa).
    """
    print(f"  Empresa: {empresa['nome']} ({empresa['id_pessoa']})")

    abrir_processos_da_empresa(pagina_empresa, empresa, referer=referer)

    # A partir daqui, pagina_empresa está na lista de processos da
    # empresa -- usamos essa URL como referer para os links de
    # detalhe de cada processo individual (mesmo mecanismo de
    # proteção provavelmente se aplica).
    url_lista_processos = pagina_empresa.url

    processos = listar_processos_da_empresa(pagina_empresa)
    print(f"    {len(processos)} processo(s) na listagem.")

    candidatos = [p for p in processos if processo_bate_filtro_preliminar(p, config)]
    print(f"    {len(candidatos)} passaram no filtro preliminar (autor/réu/data).")

    salvos = 0

    for processo_lista in candidatos:
        try:
            aguardar_entre_requisicoes()
            pagina_empresa.goto(processo_lista["url"], timeout=60_000, referer=url_lista_processos)
            pagina_empresa.wait_for_load_state("networkidle", timeout=TIMEOUT_PADRAO_MS)

            detalhe = extrair_detalhe_processo(pagina_empresa)

            # Combina os dados da listagem (autor, réu, data, classe)
            # com os do detalhe (valor da causa, OAB) -- detalhe tem
            # prioridade quando os dois têm o mesmo campo preenchido.
            dados = {**processo_lista, **{k: v for k, v in detalhe.items() if v}}
            dados["id_pessoa_autor"] = empresa["id_pessoa"]

            if processo_bate_com_filtros(dados, config):
                inserido = db.salvar_processo({
                    "numero_processo": dados.get("numero_processo") or processo_lista["url"],
                    "url": dados.get("url"),
                    "id_pessoa_autor": empresa["id_pessoa"],
                    "autor": dados.get("autor"),
                    "reu": dados.get("reu"),
                    "cnpj_autor": dados.get("cnpj_autor"),
                    "cnpj_reu": dados.get("cnpj_reu"),
                    "classe_processual": dados.get("classe_processual"),
                    "data_autuacao": dados.get("data_autuacao"),
                    "valor_causa": _parse_valor_brl(dados.get("valor_causa")),
                    "oab_numero": dados.get("oab"),
                    "oab_uf": (dados.get("oab") or "")[:2] or None,
                    "dados_brutos": str(dados),
                })
                if inserido:
                    salvos += 1
                    print(f"    ✓ Processo salvo: {dados.get('numero_processo')}")

        except PlaywrightTimeoutError:
            print(f"    ✗ Timeout processando {processo_lista.get('url')}, pulando.")
            continue

    db.marcar_empresa_processada(empresa["id_pessoa"], len(processos))
    print(f"  -> {salvos} processo(s) salvos de {len(processos)} encontrados.")


def executar_ciclo(page, pagina_empresa, db: Database, config: dict):
    """
    Um ciclo completo: percorre todos os termos de busca configurados.

    Cada empresa encontrada é processada IMEDIATAMENTE (usando uma aba
    AUXILIAR reutilizável -- `pagina_empresa`, criada uma única vez em
    main() --, via callback passado a buscar_empresas_por_termo), sem
    esperar a paginação inteira da lista de empresas terminar primeiro.
    A aba principal (`page`) nunca sai da tela de busca/paginação --
    só a aba auxiliar navega para cada empresa e seus processos.

    IMPORTANTE (corrigido após observação em produção): a versão
    anterior abria e FECHAVA uma aba nova para CADA empresa -- com
    milhares de empresas, isso sobrecarregava o navegador (memória/CPU
    de criar e destruir abas repetidamente), causando timeouts em
    cascata. Agora a mesma aba auxiliar é reaproveitada, só navegando
    para URLs diferentes -- muito mais leve, mantendo o mesmo
    benefício de nunca perturbar a paginação da aba principal.
    """
    for termo in config["termos_busca"]:
        print(f"\n=== Termo de busca: '{termo}' ===")

        abrir_consulta_processual(page)
        selecionar_tipo_pesquisa_nome_da_parte(page)

        def processar_se_necessario(empresa):
            if db.empresa_ja_processada(empresa["id_pessoa"]):
                return  # dedupe -- já visitada em ciclo anterior

            db.registrar_empresa(empresa["id_pessoa"], empresa["nome"], empresa.get("cpf_cnpj"))
            db.salvar_checkpoint(
                termo_busca_atual=termo,
                id_pessoa_empresa_atual=empresa["id_pessoa"],
            )

            try:
                processar_empresa(pagina_empresa, db, empresa, config, referer=page.url)
            except Exception as e:
                print(f"  ✗ Erro processando empresa {empresa['id_pessoa']}: {e}")

        empresas = buscar_empresas_por_termo(page, termo, ao_encontrar_empresa=processar_se_necessario)
        print(f"{len(empresas)} empresa(s) encontrada(s) para '{termo}'.")


def main():
    validar_configuracao()
    config = carregar_configuracao_filtros()

    db = Database()
    db.criar_schema()

    intervalo = int(os.environ.get("CRAWLER_INTERVALO_CICLO_SEGUNDOS", "3600"))

    with sync_playwright() as p:
        browser, context, page = abrir_sessao(p)
        # Aba auxiliar única, reaproveitada para processar todas as
        # empresas do ciclo -- ver docstring de executar_ciclo.
        pagina_empresa = context.new_page()
        pagina_empresa.set_default_timeout(TIMEOUT_PADRAO_MS)

        try:
            while True:
                print("\n########## Iniciando novo ciclo de varredura ##########")
                executar_ciclo(page, pagina_empresa, db, config)
                print(f"\nCiclo concluído. Aguardando {intervalo}s até o próximo ciclo...")
                time.sleep(intervalo)
        finally:
            browser.close()


if __name__ == "__main__":
    main()