# scraping/services/test_fetch_routes.py

import json
import os
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

BASE_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.append(str(BASE_DIR))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")

import django
django.setup()

import httpx
from django.conf import settings
from playwright.sync_api import sync_playwright

from scraping.services.puzzle_session import BASE_URL, puzzle_login

FULL_TARGET_URL = (
    f"{BASE_URL}/reports/promoters/fixed-routes-list?draw=1&columns%5B0%5D%5Bdata%5D=pearson_fullname&columns%5B0%5D%5Bname%5D=pearson_fullname&columns%5B0%5D%5Bsearchable%5D=true&columns%5B0%5D%5Borderable%5D=true&columns%5B0%5D%5Bsearch%5D%5Bvalue%5D=&columns%5B0%5D%5Bsearch%5D%5Bregex%5D=false&columns%5B1%5D%5Bdata%5D=week&columns%5B1%5D%5Bname%5D=week&columns%5B1%5D%5Bsearchable%5D=true&columns%5B1%5D%5Borderable%5D=true&columns%5B1%5D%5Bsearch%5D%5Bvalue%5D=&columns%5B1%5D%5Bsearch%5D%5Bregex%5D=false&columns%5B2%5D%5Bdata%5D=day_of_week&columns%5B2%5D%5Bname%5D=day_of_week&columns%5B2%5D%5Bsearchable%5D=true&columns%5B2%5D%5Borderable%5D=true&columns%5B2%5D%5Bsearch%5D%5Bvalue%5D=&columns%5B2%5D%5Bsearch%5D%5Bregex%5D=false&columns%5B3%5D%5Bdata%5D=company_fullname&columns%5B3%5D%5Bname%5D=company_fullname&columns%5B3%5D%5Bsearchable%5D=true&columns%5B3%5D%5Borderable%5D=true&columns%5B3%5D%5Bsearch%5D%5Bvalue%5D=&columns%5B3%5D%5Bsearch%5D%5Bregex%5D=false&columns%5B4%5D%5Bdata%5D=industry_fullname&columns%5B4%5D%5Bname%5D=industry_fullname&columns%5B4%5D%5Bsearchable%5D=true&columns%5B4%5D%5Borderable%5D=true&columns%5B4%5D%5Bsearch%5D%5Bvalue%5D=&columns%5B4%5D%5Bsearch%5D%5Bregex%5D=false&columns%5B5%5D%5Bdata%5D=km&columns%5B5%5D%5Bname%5D=km&columns%5B5%5D%5Bsearchable%5D=true&columns%5B5%5D%5Borderable%5D=true&columns%5B5%5D%5Bsearch%5D%5Bvalue%5D=&columns%5B5%5D%5Bsearch%5D%5Bregex%5D=false&columns%5B6%5D%5Bdata%5D=travel_time&columns%5B6%5D%5Bname%5D=travel_time&columns%5B6%5D%5Bsearchable%5D=true&columns%5B6%5D%5Borderable%5D=true&columns%5B6%5D%5Bsearch%5D%5Bvalue%5D=&columns%5B6%5D%5Bsearch%5D%5Bregex%5D=false&order%5B0%5D%5Bcolumn%5D=1&order%5B0%5D%5Bdir%5D=ASC&start=0&length=10&search%5Bvalue%5D=&search%5Bregex%5D=false&with_promoters_id=%5B%5D&with_regional_id=%5B%5D&with_sale_point_id=%5B%5D&with_companybrand_id=%5B%5D&with_supervisor_id=%5B%5D&with_industry_id=%5B%5D&_=1790363486735"
)

JSON_DIR = Path(settings.MEDIA_ROOT) / "json_exports"
JSON_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_JSON_PATH = JSON_DIR / "fixed_routes.json"


def extract_routes_dynamically():
    print("--- 1. Efetuando Login para capturar Cookies da Sessão ---")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()

        try:
            puzzle_login(page)

            # Mapeia os cookies para o cliente HTTP
            cookies_list = context.cookies()
            formatted_cookies = {
                c["name"]: c["value"] for c in cookies_list
            }

            # Extrai os parâmetros base da URL original fornecida
            parsed_url = urlparse(FULL_TARGET_URL)
            params = {
                k: v[0] for k, v in parse_qs(parsed_url.query).items()
            }
            api_endpoint = (
                f"{parsed_url.scheme}://{parsed_url.netloc}{parsed_url.path}"
            )

            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
                    " AppleWebKit/537.36"
                ),
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "X-Requested-With": "XMLHttpRequest",
            }

            with httpx.Client(
                cookies=formatted_cookies, headers=headers, timeout=60.0
            ) as client:
                # PASSO A: PRIMEIRA REQUISIÇÃO (Leve - Pega apenas 1 registro para ler recordsTotal)
                print(
                    "--- 2. Consultando total de registros (recordsTotal) ---"
                )
                params["length"] = "1"
                resp_check = client.get(api_endpoint, params=params)

                if resp_check.status_code != 200:
                    print(
                        f"Erro ao consultar API: Status {resp_check.status_code}"
                    )
                    return

                data_check = resp_check.json()
                total_records = data_check.get("recordsTotal", 0)

                print(
                    f"-> Total de rotas fixas identificado no servidor:"
                    f" {total_records}"
                )

                if total_records == 0:
                    print(
                        "Nenhuma rota encontrada ou a sessão não autenticou"
                        " corretamente."
                    )
                    return

                # PASSO B: SEGUNDA REQUISIÇÃO (Traz 100% dos registros de uma vez)
                print(
                    f"--- 3. Baixando 100% dos {total_records} registros ---"
                )
                params["length"] = str(total_records)
                resp_full = client.get(api_endpoint, params=params)

                if resp_full.status_code == 200:
                    full_data = resp_full.json()
                    routes_list = full_data.get("data", [])

                    # Salvar arquivo JSON final
                    with open(OUTPUT_JSON_PATH, "w", encoding="utf-8") as f:
                        json.dump(
                            routes_list,
                            f,
                            ensure_ascii=False,
                            indent=4,
                            default=str,
                        )

                    print(
                        f"\nSUCESSO! {len(routes_list)} rotas capturadas e"
                        f" salvas em:\n{OUTPUT_JSON_PATH}"
                    )
                else:
                    print(
                        f"Erro ao obter massa completa: Status"
                        f" {resp_full.status_code}"
                    )

        except Exception as e:
            print(f"Erro na extração dinâmica de rotas: {e}")

        finally:
            browser.close()


if __name__ == "__main__":
    extract_routes_dynamically()