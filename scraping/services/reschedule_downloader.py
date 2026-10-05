# scraping/services/reschedule_downloader.py

import os
import time
from pathlib import Path
from django.conf import settings
from playwright.sync_api import sync_playwright

class PuzzleRescheduleDownloader:
    def __init__(self, base_url="https://hipertrade-puzzle-op.techtd.com.br"):
        self.base_url = base_url
        self.download_dir = Path(settings.MEDIA_ROOT) / "downloads"
        self.download_dir.mkdir(parents=True, exist_ok=True)

    def execute_flow(self, username, password):
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False) 
            context = browser.new_context(accept_downloads=True)
            page = context.new_page()

            try:
                # 1. Login
                page.goto(f"{self.base_url}/login", wait_until="networkidle")

                page.wait_for_selector('input[name="email"]', state="visible", timeout=15000)
                page.fill('input[name="email"]', username)
                page.fill('input[name="password"]', password)
                page.click('button[type="submit"]')
                page.wait_for_load_state("networkidle")

                # 2. NAVEGAÇÃO REAL
                # Substitua pela URL exata da tela onde fica a planilha/exportação no Puzzle:
                page.goto(f"{self.base_url}/dashboards/indicators-contracts", wait_until="networkidle")

                # 3. Download da Planilha
                with page.expect_download(timeout=60000) as download_info:
                    # Ajuste com o texto ou id do botão real de exportação
                    page.click('text="Exportar Planilha"') 

                download = download_info.value
                file_path = self.download_dir / download.suggested_filename
                download.save_as(file_path)

                return {
                    "status": "success",
                    "file_path": str(file_path),
                    "cookies": context.cookies()
                }

            except Exception as e:
                return {
                    "status": "error",
                    "message": str(e)
                }
            finally:
                browser.close()