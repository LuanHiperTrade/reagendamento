# scraping/services/test_export.py

import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.append(str(BASE_DIR))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")

import django
django.setup()

from django.conf import settings
from playwright.sync_api import sync_playwright

from scraping.services.puzzle_session import BASE_URL, puzzle_login

CONTRACTS_URL = f"{BASE_URL}/dashboards/indicators-contracts"

DOWNLOAD_DIR = Path(settings.MEDIA_ROOT) / "downloads"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

def parse_notification_time(time_str: str) -> datetime:
    """
    Função auxiliar para converter o texto de data/hora da notificação em um objeto datetime.
    Ajuste este parser de acordo com o formato retornado pela tela (ex: '25/09/2026 15:10' ou 'há 2 minutos').
    """
    # Exemplo tratando formato 'DD/MM/YYYY HH:MM':
    try:
        return datetime.strptime(time_str.strip(), "%d/%m/%Y %H:%M")
    except ValueError:
        # Se o formato for relativo ou diferente, tratamos o fallback
        return datetime.now()

def test_export():
    print("--- Iniciando processo de exportação e monitoramento de notificações ---")
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=500)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()

        try:
            # 1. Login
            print("1. Efetuando login...")
            puzzle_login(page)

            # 2. Acessar tela de Contratos
            print(f"2. Acessando {CONTRACTS_URL}...")
            page.goto(CONTRACTS_URL, wait_until="networkidle")

            # Guardar o momento exato antes de solicitar a exportação
            request_time = datetime.now() - timedelta(seconds=10) # margem de 10s de tolerância
            print(f"3. Horário do disparo da solicitação: {request_time.strftime('%H:%M:%S')}")

            # 3. Clicar no botão para solicitar a exportação
            # TODO: Ajuste o seletor do botão de solicitar exportação
            page.click('text="Exportar"')
            print("   Solicitação enviada no Puzzle!")

            # 4. Loop de checagem da notificação
            max_attempts = 12   # Até 12 tentativas
            delay_seconds = 10  # Espera 10 segundos entre cada checagem (total ~2 min de limite)
            download_triggered = False
            file_path = None

            for attempt in range(1, max_attempts + 1):
                print(f"\n[Tentativa {attempt}/{max_attempts}] Checando notificações...")

                # 1. Abrir central de notificações
                notification_btn = page.locator('#read_notify')
                
                if notification_btn.is_visible():
                    notification_btn.click()
                    page.wait_for_timeout(1000)

                    # 2. Localizar os links de download dentro do dropdown
                    dropdown_menu = page.locator('.dropdown-menu.show, #read_notify + .dropdown-menu, .notification-dropdown')
                    
                    # Procura por qualquer link que tenha 'amazonaws.com' ou 'http' no href
                    links = dropdown_menu.locator('a[href*="amazonaws.com"], a[href*="http"]')
                    
                    if links.count() > 0:
                        first_link = links.first
                        download_url = first_link.get_attribute('href')

                        print(f"   [URL DE DOWNLOAD ENCONTRADA]:\n   {download_url[:100]}...")

                        # 3. Baixar o arquivo diretamente via requisição HTTP do próprio contexto
                        print("   -> Baixando arquivo via requisição direta...")
                        response = page.request.get(download_url)

                        if response.ok:
                            # Nome padrão para a planilha
                            file_path = DOWNLOAD_DIR / "Indicador_de_contratos.xlsx"
                            
                            # Salva os bytes do arquivo em disco
                            with open(file_path, "wb") as f:
                                f.write(response.body())

                            download_triggered = True
                            print(f"\nSUCESSO! Planilha baixada e salva com sucesso em:\n{file_path}")
                            break
                        else:
                            print(f"   Erro ao baixar via URL (Status {response.status}).")
                    else:
                        print("   Notificação ainda não possui link de download disponível.")
                else:
                    print("   Botão #read_notify não encontrado.")

                # Se não encontrou o link, recarrega e tenta novamente
                print(f"   Aguardando {delay_seconds}s para recarregar a página...")
                time.sleep(delay_seconds)
                page.reload(wait_until="networkidle")

        except Exception as e:
            print(f"\nErro no fluxo de notificação/download: {e}")

        finally:
            page.wait_for_timeout(3000)
            browser.close()

if __name__ == "__main__":
    test_export()
