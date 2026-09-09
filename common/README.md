# Automação e-Proc (multi-tribunal) com Playwright

Código COMPARTILHADO por todos os tribunais (ver `../README.md` pra
entender a estrutura de pastas -- este `common/` não roda sozinho, é
usado pelo Dockerfile de cada `TJXX/`). Reescrita em Python do fluxo
que estava sendo montado no Power Automate Desktop. Reaproveita tudo
que já foi validado manualmente (login, seletores, values do
multiselect de Classe Processual).

Nasceu para o TJRS, mas o login detecta sozinho o fluxo certo (Keycloak/SSO
ou formulário nativo do e-Proc) e funciona em qualquer tribunal apontado por
`EPROC_URL` -- confirmado em uso real contra o TJTO além do TJRS; TJRO deve
funcionar pelo mesmo mecanismo, mas ainda sem confirmação com uma conta real.

## Rodando com Docker (recomendado)

Todo comando abaixo roda de DENTRO da pasta do tribunal desejado (ex.:
`TJRS/`, `TJRO/`, `TJTO/`), não daqui de `common/` -- é lá que ficam o
`Dockerfile`, o `docker-compose.yaml` e o `.env` daquele tribunal
específico (ver `../README.md`).

O container fica de pé (não executa nada sozinho), permitindo entrar
nele e rodar os scripts manualmente na ordem que precisar -- útil para
primeiro gerar/decodificar o QR code do MFA e só depois rodar a
automação.

```bash
cd ../TJRS   # ou TJRO/TJTO -- qualquer pasta de tribunal
cp .env.example .env
# edite o .env com suas credenciais reais (pode deixar EPROC_TOTP_SECRET
# em branco por enquanto, se ainda for gerar o QR code)

mkdir -p qrcodes output crawler_config   # pastas usadas para trocar arquivos com o container

docker compose build
docker compose up -d      # sobe o container + o mysql próprio dessa pasta, em segundo plano
```

**1. Gerar/obter o QR code do MFA** (fora do container, no navegador):
   siga os passos da seção "MFA / código rotativo" abaixo -- desvincule
   e recadastre o MFA no e-Proc, salvando um print do QR code exibido.
   Salve essa imagem em `./qrcodes/qrcode.png` (a pasta já está mapeada
   para dentro do container).

**2. Entrar no container e decodificar o QR code:**
```bash
docker compose exec eproc-automation bash
# já dentro do container:
python decode_qr.py qrcodes/qrcode.png
```
   Copie o `TOTP_SECRET` impresso e cole em `EPROC_TOTP_SECRET` no
   `.env` (no host, fora do container).

**3. Reiniciar o container para carregar o `.env` atualizado e rodar
   a automação:**
```bash
# fora do container:
docker compose up -d --force-recreate

docker compose exec eproc-automation bash
# já dentro do container:
python eproc_automation.py
```

Screenshots de erro (`erro_timeout.png` / `erro_geral.png`) aparecem em
`./output` no host. Para depurar visualmente os seletores, defina
`EPROC_HEADLESS=false` no `.env` -- mas como o container não tem
interface gráfica, prefira rodar sem Docker (seção abaixo) quando
precisar ver o navegador na tela.

## Rodando localmente (sem Docker)

```bash
pip install -r requirements.txt
playwright install chromium

cp ../TJRS/.env.example .env   # ou TJRO/TJTO -- qualquer .env.example de tribunal
# edite o .env com suas credenciais reais

python eproc_automation.py
```

## Configuração

Todas as configurações agora vêm do arquivo `.env` (veja `.env.example`):

- `EPROC_USUARIO` / `EPROC_SENHA`
- `EPROC_TOTP_SECRET` (veja seção "MFA / código rotativo" abaixo)
- `EPROC_NOME_DA_PARTE`
- `EPROC_HEADLESS` (`true` = segundo plano, `false` = janela visível)

`CLASSES_PROCESSUAIS` continua fixo no código (dicionário value ->
descrição), já vem preenchido com MONITÓRIA e Embargos Parciais à Ação
Monitória -- edite direto em `eproc_automation.py` se precisar de
outras classes.

O script falha imediatamente com uma mensagem clara se `EPROC_USUARIO`,
`EPROC_SENHA` ou `EPROC_TOTP_SECRET` não estiverem definidos, em vez de
travar no meio do login.

## MFA / código rotativo (TOTP)

O script gera o código de 6 dígitos automaticamente com a biblioteca
`pyotp`, sem precisar do celular. Para isso, você precisa da **chave
secreta Base32** que fica por trás do QR code do seu app autenticador
(algo como `JBSWY3DPEHPK3PXP`).

**Como obter a chave**, se ainda não tem:
1. **Com a imagem do QR code** (print de tela salvo, ou foto): use o
   script auxiliar incluído `decode_qr.py`:
   ```bash
   python decode_qr.py caminho/para/qrcode.png
   ```
   Ele extrai e imprime o `TOTP_SECRET` -- cole esse valor em
   `EPROC_TOTP_SECRET` no seu `.env`, sem precisar escanear com o
   celular.
2. No app autenticador (Google/Microsoft Authenticator, Authy etc.),
   veja também se há opção de "exportar" ou "ver chave" da entrada do
   e-Proc -- alguns apps mostram a chave em texto diretamente.
3. Se não for possível recuperar de nenhuma forma, desvincule o MFA
   atual e cadastre um novo no e-Proc. Na tela de configuração, **antes
   de escanear com o celular**, tire um print/salve a imagem do QR code
   exibido na tela e rode o `decode_qr.py` nela -- ou procure o link
   "não consigo escanear o QR code", que costuma mostrar a chave em
   texto puro.

Cole essa chave em `TOTP_SECRET`.

⚠️ **Segurança:** essa chave nunca expira (diferente do código de 6
dígitos, que muda a cada 30s). Quem tiver acesso a ela consegue gerar
seus códigos MFA indefinidamente -- trate com o mesmo cuidado que a
senha. O `.env` já está no `.gitignore`/`.dockerignore`, então não é
versionado nem entra na imagem Docker -- mesmo assim, nunca compartilhe
esse arquivo.

## Executar

```bash
python eproc_automation.py
```

## Partes já validadas (não deveriam precisar de ajuste)

Login e MFA foram confirmados via captura de tráfego real no Burp Suite.
O e-Proc não tem um único fluxo de login -- alguns tribunais usam SSO via
Keycloak (TJRS), outros um formulário nativo próprio do e-Proc (TJTO) --
`login()` detecta sozinho qual formulário está na tela e escolhe o fluxo
certo, sem precisar saber de antemão qual o tribunal usa (ver docstring de
`login()`/`_login_keycloak()`/`_login_nativo()` no código):

- Login (Keycloak, ex. TJRS): `#username`, `#password`, botão `#kc-login`
- MFA (Keycloak): `#otp`, checkbox `#saveDevice`, botão `#kc-login`
- Login nativo (ex. TJTO): `#txtUsuario`, `#pwdSenha` (mascarado -- exige
  digitação simulada, ver docstring de `_login_nativo()`), botão `#sbmEntrar`
- MFA nativo: `#txtAcessoCodigo`, botão `#btnValidar` -- aparece na MESMA
  página, sem navegação, diferente do MFA do Keycloak
- Menu de navegação: `a[aria-label='Consulta Processual']` (abre
  submenu) → `a[aria-label='Consultar Processos']` (link final)
- Dropdown Tipo de Pesquisa: `#selTipoPesquisa`, valor `"NO"` = Nome da Parte
- **Campo Nome da Parte**: `input[name='strNomeParte']` -- confirmado
  via captura do corpo real da consulta (`strNomeParte=Agro+LT+DA`, o
  `+` é só a codificação de espaço do form-urlencoded; use a string
  normal com espaços em `EPROC_NOME_DA_PARTE`, o Playwright cuida da
  codificação). É um campo de autocomplete (dispara uma busca AJAX
  `acao_ajax=processos_consulta_por_nome_parte` conforme você digita),
  mas a consulta funciona com o texto literal preenchido, sem precisar
  clicar em nenhuma sugestão.
- Multiselect Classe Processual:
  - Container: `#divClasseProcessual div.ms-parent.classeMultipleSelect`
  - Botão: `.ms-choice`
  - Checkboxes: `input[name="selectItemselIdClasse"][value="..."]`
  - Confirmado (via captura) que múltiplas classes podem ser marcadas
    ao mesmo tempo -- o e-Proc atualiza sozinho os campos ocultos
    `selIdClasse` e `selIdClasseSelecionados` (lista separada por
    vírgula) via JavaScript próprio da página; não precisamos mexer
    neles diretamente.
- **Botão "Consultar" final**: `page.get_by_role("button", name="Consultar", exact=True)`.
  O seletor original (`text=Consultar`) causou um bug real em execução:
  ele batia com itens do menu de navegação que também contêm a palavra
  "Consultar" (ex.: "Consultar Alvará Eletrônico Automatizado"), e o
  Playwright tentava clicar no primeiro (escondido), estourando timeout
  de 30s. Buscar pelo *role* de botão resolve isso, pois ignora `<span>`
  de texto de menu.

**Sessão persistente / MFA só na primeira vez:** no fluxo Keycloak, o
script marca o checkbox `#saveDevice` no login; no fluxo nativo não há
um checkbox equivalente. Em ambos os casos os cookies são salvos em
`output/storage_state.json` (caminho configurável via
`EPROC_STORAGE_STATE`). Nas próximas execuções, se esse arquivo existir
e a sessão ainda for válida, o login/MFA é pulado inteiramente. Apague
o arquivo (ou espere a sessão expirar) para forçar um novo login.

## Partes marcadas com `# TODO` no código -- ainda precisam de confirmação

1. **Extração dos resultados** -- ainda não implementada; equivalente às
   ações "Obter caixas de seleção marcadas" do fluxo antigo
2. **Nomes de 2 classes processuais**: os values `0000000094` e
   `0000000029` foram confirmados via captura de tráfego, mas não
   sabemos o texto exibido na tela para eles (só apareceram como
   `value`, não vimos o rótulo visual). Edite `CLASSES_PROCESSUAIS` em
   `eproc_automation.py` com a descrição correta -- isso só afeta a
   legibilidade dos logs, não o funcionamento.

## Por que isso deve ser mais estável que o Power Automate

- `page.wait_for_selector` espera de forma nativa e confiável, sem os
  "busy-wait" manuais que precisamos simular no JavaScript do PAD.
- Sem problema de múltiplas instâncias de navegador órfãs: o Playwright
  controla o ciclo de vida do browser diretamente no `with` do Python.
- Erros reais aparecem no traceback do Python, com screenshot automático
  (`erro_timeout.png` / `erro_geral.png`), em vez de `[object Object]`.
- Fácil de rodar em loop, agendar (cron / Task Scheduler) ou adaptar para
  múltiplas pesquisas a partir de uma planilha.

## Próximos passos sugeridos

- Confirmar os 2 seletores restantes marcados com `# TODO` acima
  (campo de nome da parte e botão "Consultar" final).
- Implementar a extração/exportação dos resultados da consulta.
- Se for rodar periodicamente, considerar adicionar retry simples em volta
  de `main()` para lidar com instabilidades de rede pontuais.

## Crawler de varredura em massa (`run_crawler.py`)

Módulos adicionais além do `eproc_automation.py`:

- `db.py` -- schema MySQL (empresas, processos, checkpoint) com dedupe
- `crawler.py` -- paginação DataTables, extração de empresas e processos,
  filtros (preliminar + completo)
- `run_crawler.py` -- loop contínuo, orquestra tudo

### Todos os seletores confirmados via HTML real

- Lista de empresas: `#divInfraAreaTabela` (linhas com `data-idpessoa`)
- Lista de processos de uma empresa: **mesmo** `#divInfraAreaTabela`,
  colunas Nº Processo / Data de Autuação / Juízo / Autor / Réu / Classe
  Judicial / Último Evento / Assunto(s) / Situação
- Detalhe do processo: `#txtNumProcesso`, `#txtClasse`, `#txtAutuacao`,
  Valor da Causa (busca por rótulo em `#fldInformacoesAdicionais_content`),
  OAB (regex `[A-Z]{2}\d{6}` no bloco de Partes e Representantes)
- Paginação: padrão DataTables (`#divInfraAreaTabela_next`,
  `_paginate`, `_length`) -- reaproveitado em toda tela de resultado
  do e-Proc, então a mesma função `paginar_datatable()` serve para
  empresas e para processos

### Otimização: filtro em duas etapas

A listagem de processos já traz Autor, Réu, Data de Autuação e Classe
-- só Valor da Causa e OAB exigem abrir o detalhe de cada processo.
`processar_empresa()` primeiro aplica um filtro preliminar (autor/réu/
data) usando só os dados da listagem, e só abre o detalhe dos
processos que passam nessa primeira peneira. Evita navegações
desnecessárias em escala.

### Rodando o crawler

```bash
docker compose exec eproc-automation bash
python run_crawler.py
```

Roda em loop contínuo (ver `CRAWLER_INTERVALO_CICLO_SEGUNDOS` no
`.env`), salvando progresso e resultados no MySQL. Pode ser
interrompido e retomado a qualquer momento -- o checkpoint garante que
empresas já processadas não sejam repetidas.
