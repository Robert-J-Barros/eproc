# Imagem oficial do Playwright: já vem com Chromium, Firefox, WebKit e
# todas as dependências de sistema necessárias -- evita o clássico
# problema de "faltou lib X" ao rodar navegador em container.
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

WORKDIR /app

# O código de automação é COMPARTILHADO entre todos os tribunais (ver
# ../common) -- só a config (.env) e o banco mudam por tribunal. Por
# isso o build context deste serviço é a RAIZ do repositório, não esta
# pasta (ver "context: .." no docker-compose.yaml ao lado) -- é o que
# permite copiar de common/ aqui.
COPY common/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY common/ .

# Variáveis sensíveis (usuário, senha, TOTP secret, credenciais do
# banco) vêm do .env desta pasta em tempo de execução (ver
# docker-compose.yaml), nunca "bakadas" na imagem.
ENV PYTHONUNBUFFERED=1

# Mantém o container vivo em vez de rodar o script automaticamente --
# permite entrar com `docker exec` e rodar decode_qr.py /
# eproc_automation.py manualmente, na ordem que precisar (ex.: gerar o
# QR code primeiro).
CMD ["tail", "-f", "/dev/null"]
