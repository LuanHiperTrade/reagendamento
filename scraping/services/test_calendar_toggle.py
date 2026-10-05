# scraping/services/test_calendar_toggle.py

# ATENÇÃO: este script ALTERA a rota real no Puzzle. Sem --confirmar ele não salva.
#   python scraping/services/test_calendar_toggle.py            -> simula (não clica em salvar)
#   python scraping/services/test_calendar_toggle.py --confirmar -> ativa a marca e salva

import os
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.append(str(BASE_DIR))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")

import django
django.setup()

from playwright.sync_api import sync_playwright

from scraping.services.puzzle_session import BASE_URL, puzzle_login

PEARSON_ID = "11574"
TARGET_BRAND = "ARTHI"
CONFIRMAR = "--confirmar" in sys.argv


def test_calendar_toggle():
    print(f"--- Testando Ativação e Salvamento no Calendário ({PEARSON_ID}) ---")

    today_str = datetime.now().strftime("%d/%m/%Y")
    print(f"Buscando rota para a data de hoje: {today_str}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=1000)
        context = browser.new_context()
        page = context.new_page()

        try:
            # 1. Login
            print("1. Efetuando login...")
            puzzle_login(page)

            # 2. Acessar o Calendário do Promotor
            calendar_url = f"{BASE_URL}/itineraries/calendar/{PEARSON_ID}"
            print(f"2. Acessando o Calendário: {calendar_url}")
            page.goto(calendar_url, wait_until="networkidle")

            # 3. Localizar o bloco da data de hoje
            print(f"3. Procurando o bloco 'Rota dia {today_str}'...")
            date_locator = page.locator(f'p:has-text("Rota dia {today_str}")')

            if date_locator.is_visible():
                print("   Data de hoje encontrada no calendário!")

                # Sobe para o elemento pai e clica em 'Editar Rota'
                edit_button = date_locator.locator("xpath=..").locator(
                    'a[title="Editar Rota"], a:has(i.fa-edit)'
                )

                if edit_button.is_visible():
                    print("4. Clicando em 'Editar Rota'...")
                    edit_button.click()
                    page.wait_for_load_state("networkidle")
                    print(f"   Tela de edição carregada: {page.url}")

                    # 5. Localizar a <li> exata da marca ARTHI
                    print(f"5. Procurando o elemento da marca '{TARGET_BRAND}'...")

                    # Busca por elementos <li> cujo texto contenha especificamente a marca
                    # Usamos filter(has_text=...) para refinar a busca no Playwright
                    brand_li = page.locator('li').filter(has_text=TARGET_BRAND)

                    if brand_li.count() > 0:
                        # Pega o primeiro item específico correspondente à marca
                        target_li = brand_li.first
                        print(f"   Linha exata da marca '{TARGET_BRAND}' localizada!")

                        # Localiza o botão .reEnable específico dentro desta <li>
                        enable_btn = target_li.locator('a.reEnable').first

                        if not CONFIRMAR:
                            estado = "desativada" if enable_btn.is_visible() else "ativa"
                            print(f"   [SIMULAÇÃO] Marca '{TARGET_BRAND}' está {estado}. Nada foi clicado nem salvo.")
                            print("   Rode com --confirmar para ativar e salvar no Puzzle.")
                            return

                        if enable_btn.is_visible():
                            print(f"   Ativando/Ligando a marca '{TARGET_BRAND}'...")
                            enable_btn.click()
                            page.wait_for_timeout(1000)
                            print("   Marca ativada com sucesso!")
                        else:
                            print(f"   Aviso: O botão .reEnable da marca '{TARGET_BRAND}' não está visível (marca pode já estar ativa).")

                        # 6. Salvar as alterações
                        print("6. Clicando no botão de salvar (#btn-submit)...")
                        save_button = page.locator('#btn-submit')

                        if save_button.is_visible():
                            save_button.click()
                            page.wait_for_load_state("networkidle")
                            print("\nSUCESSO! Alteração salva no Puzzle.")
                        else:
                            print("   Aviso: Botão #btn-submit não foi encontrado.")
                    else:
                        print(f"   Marca '{TARGET_BRAND}' não foi encontrada na lista.")

        except Exception as e:
            print(f"\nErro no processo do calendário: {e}")

        finally:
            print("Aguardando 5 segundos antes de fechar...")
            page.wait_for_timeout(5000)
            browser.close()


if __name__ == "__main__":
    test_calendar_toggle()