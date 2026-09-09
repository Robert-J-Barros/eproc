# Automação e-Proc -- multi-tribunal

Estrutura do repositório:

```
common/          código compartilhado por TODOS os tribunais (login,
                 filtros, crawler, banco) -- ver common/README.md para
                 os detalhes de cada seletor/fluxo validado
TJRS/            stack independente do TJRS (Dockerfile, docker-compose.yaml, .env)
TJRO/            stack independente do TJRO
TJTO/            stack independente do TJTO
```

**Por que separado assim:** o código de automação é o mesmo pra
qualquer tribunal e-Proc -- só muda a URL, as credenciais e o banco.
Em vez de duplicar os `.py` em cada pasta (bug corrigido num precisaria
ser replicado manualmente em todo lugar), cada `TJXX/` só tem:

- `Dockerfile` -- copia o código de `../common` na hora do build (o
  build context é a raiz do repositório, não a própria pasta -- veja
  `context: ..` no `docker-compose.yaml`).
- `docker-compose.yaml` -- dois serviços: `eproc-automation` (o
  container que roda os scripts) e `mysql` (banco 100% próprio dessa
  pasta, com volume nomeado só dela -- nenhum tribunal compartilha
  linha de banco com outro).
- `.env` (a partir de `.env.example`) -- credenciais e configuração
  daquele tribunal especificamente.
- `output/`, `qrcodes/`, `crawler_config/` -- arquivos de troca com o
  host, isolados por tribunal.

## Rodando um tribunal

Todo comando roda de DENTRO da pasta do tribunal, não da raiz:

```bash
cd TJRS
cp .env.example .env
# edite o .env com as credenciais reais desse tribunal

mkdir -p qrcodes output crawler_config   # se ainda não existirem
docker compose build
docker compose up -d     # sobe o container da automação + o mysql próprio
```

Depois disso, o fluxo (gerar/decodificar QR code do MFA, rodar
`eproc_automation.py`/`run_crawler.py`, etc.) é exatamente o descrito
em `common/README.md` -- só que executado dentro do container daquele
tribunal (`docker compose exec eproc-automation bash`).

Cada `docker compose up -d`/`down`/`logs` age só sobre a stack da pasta
atual -- pode ter TJRS, TJRO e TJTO rodando ao mesmo tempo sem
interferência (bancos, sessões e containers são todos independentes).
As portas do MySQL publicadas no host também são distintas por
tribunal (TJRS=3306, TJRO=3307, TJTO=3308), pra poder inspecionar
qualquer um dos bancos de fora (DBeaver, MySQL Workbench etc.) com
todos rodando ao mesmo tempo.

## Adicionando um tribunal novo

Não mexe em `common/`. Só duplica uma pasta de tribunal existente:

```bash
cp -r TJRS TJ<NOVO>
cd TJ<NOVO>
rm -rf output/* qrcodes/* .env   # começa limpo, sem sessão/segredos de outro tribunal
```

Depois edite `docker-compose.yaml` trocando os nomes (`container_name`,
nome do volume, porta do host publicada) pra não colidir com os outros
tribunais já em uso, e preencha o `.env` com a URL/credenciais do
tribunal novo. Se o login usar um fluxo diferente dos dois já
suportados (Keycloak/SSO ou formulário nativo -- ver `login()` em
`common/eproc_automation.py`), vai precisar de uma investigação rápida
ao vivo antes: rode com `EPROC_HEADLESS=false` (fora do Docker, que não
tem tela) pra ver o formulário real e ajustar os seletores.
