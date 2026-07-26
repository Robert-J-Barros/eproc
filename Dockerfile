# Imagem oficial do Playwright: já vem com Chromium, Firefox, WebKit e
# todas as dependências de sistema necessárias -- evita o clássico
# problema de "faltou lib X" ao rodar navegador em container.
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

WORKDIR /app

# Copia e instala dependências Python primeiro (aproveita cache do
# Docker -- só reinstala se requirements.txt mudar)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copia o restante do código
COPY . .

# Variáveis sensíveis devem ser passadas em tempo de execução
# (docker run --env-file .env, ou via docker-compose), nunca "bakadas"
# na imagem. Veja .env.example.
ENV PYTHONUNBUFFERED=1

# Mantém o container vivo em vez de rodar o script automaticamente --
# permite entrar com `docker exec` e rodar decode_qr.py / eproc_automation.py
# manualmente, na ordem que precisar (ex.: gerar o QR code primeiro).
CMD ["tail", "-f", "/dev/null"]
