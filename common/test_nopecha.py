from pathlib import Path
from playwright.sync_api import sync_playwright

NOPECHA_DIR = Path("extensions/NopeCha").resolve()

with sync_playwright() as p:
    context = p.chromium.launch_persistent_context(
        user_data_dir="profile",
        headless=False,
        args=[
            f"--disable-extensions-except={NOPECHA_DIR}",
            f"--load-extension={NOPECHA_DIR}",
        ],
    )

    page = context.new_page()
    page.goto("https://nopecha.com/demo/cloudflare")

    input("Pressione ENTER para fechar...")

    context.close()