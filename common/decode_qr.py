"""
Extrai a chave secreta TOTP (Base32) de uma imagem de QR code de MFA.

Uso:
    python decode_qr.py caminho/para/imagem_do_qrcode.png

Requisitos:
    pip install opencv-python
"""

import sys
from urllib.parse import urlparse, parse_qs

import cv2


def extrair_secret(caminho_imagem: str) -> None:
    imagem = cv2.imread(caminho_imagem)
    if imagem is None:
        print(f"ERRO: não consegui abrir a imagem '{caminho_imagem}'. "
              f"Confira o caminho e o formato (png/jpg).")
        return

    detector = cv2.QRCodeDetector()
    dados, _, _ = detector.detectAndDecode(imagem)

    if not dados:
        print("ERRO: nenhum QR code encontrado na imagem. "
              "Tente um print mais nítido, sem cortar as bordas do QR.")
        return

    print(f"Conteúdo bruto do QR code:\n{dados}\n")

    if not dados.startswith("otpauth://"):
        print("Aviso: o conteúdo não parece ser um QR code de TOTP "
              "(não começa com 'otpauth://'). Copie manualmente o valor "
              "acima se for o caso.")
        return

    parsed = urlparse(dados)
    params = parse_qs(parsed.query)
    secret = params.get("secret", [None])[0]
    issuer = params.get("issuer", [None])[0]
    label = parsed.path.lstrip("/")

    if secret:
        print("=" * 50)
        print(f"Emissor (issuer): {issuer}")
        print(f"Conta (label):    {label}")
        print(f"SECRET (TOTP_SECRET): {secret}")
        print("=" * 50)
        print("\nCole o valor de SECRET acima na variável TOTP_SECRET "
              "em eproc_automation.py")
    else:
        print("ERRO: não encontrei o parâmetro 'secret' na URL do QR code.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Uso: python decode_qr.py caminho/para/imagem_do_qrcode.png")
        sys.exit(1)

    extrair_secret(sys.argv[1])
