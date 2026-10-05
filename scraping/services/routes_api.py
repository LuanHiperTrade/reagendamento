# scraping/services/routes_api.py

import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import httpx
from django.conf import settings

from scraping.services.puzzle_session import BASE_URL

DEFAULT_ROUTES_URL = (
    f"{BASE_URL}/reports/promoters/fixed-routes-list?draw=1"
    "&columns%5B0%5D%5Bdata%5D=pearson_fullname&columns%5B0%5D%5Bname%5D=pearson_fullname"
    "&columns%5B0%5D%5Bsearchable%5D=true&columns%5B0%5D%5Borderable%5D=true"
    "&columns%5B1%5D%5Bdata%5D=week&columns%5B1%5D%5Bname%5D=week"
    "&columns%5B1%5D%5Bsearchable%5D=true&columns%5B1%5D%5Borderable%5D=true"
    "&columns%5B2%5D%5Bdata%5D=day_of_week&columns%5B2%5D%5Bname%5D=day_of_week"
    "&columns%5B2%5D%5Bsearchable%5D=true&columns%5B2%5D%5Borderable%5D=true"
    "&columns%5B3%5D%5Bdata%5D=company_fullname&columns%5B3%5D%5Bname%5D=company_fullname"
    "&columns%5B3%5D%5Bsearchable%5D=true&columns%5B3%5D%5Borderable%5D=true"
    "&columns%5B4%5D%5Bdata%5D=industry_fullname&columns%5B4%5D%5Bname%5D=industry_fullname"
    "&columns%5B4%5D%5Bsearchable%5D=true&columns%5B4%5D%5Borderable%5D=true"
    "&columns%5B5%5D%5Bdata%5D=km&columns%5B5%5D%5Bname%5D=km"
    "&columns%5B5%5D%5Bsearchable%5D=true&columns%5B5%5D%5Borderable%5D=true"
    "&columns%5B6%5D%5Bdata%5D=travel_time&columns%5B6%5D%5Bname%5D=travel_time"
    "&columns%5B6%5D%5Bsearchable%5D=true&columns%5B6%5D%5Borderable%5D=true"
    "&order%5B0%5D%5Bcolumn%5D=1&order%5B0%5D%5Bdir%5D=ASC&start=0&length=10"
    "&with_promoters_id=%5B%5D&with_regional_id=%5B%5D&with_sale_point_id=%5B%5D"
    "&with_companybrand_id=%5B%5D&with_supervisor_id=%5B%5D&with_industry_id=%5B%5D"
)


def fetch_fixed_routes(cookies_dict: dict) -> str:
    """Consome a API de DataTables do Puzzle para extrair todas as rotas fixas

    usando os cookies de sessão ativos e salvando em
    media/json_exports/fixed_routes.json.
    """
    json_dir = Path(settings.MEDIA_ROOT) / "json_exports"
    json_dir.mkdir(parents=True, exist_ok=True)
    output_json_path = json_dir / "fixed_routes.json"

    # Formatação de cookies para o httpx
    formatted_cookies = {}
    if isinstance(cookies_dict, list):
        for c in cookies_dict:
            formatted_cookies[c["name"]] = c["value"]
    else:
        formatted_cookies = cookies_dict

    parsed_url = urlparse(DEFAULT_ROUTES_URL)
    params = {k: v[0] for k, v in parse_qs(parsed_url.query).items()}
    api_endpoint = f"{parsed_url.scheme}://{parsed_url.netloc}{parsed_url.path}"

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        ),
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
    }

    with httpx.Client(
        cookies=formatted_cookies, headers=headers, timeout=60.0
    ) as client:
        # 1. Primeira consulta rápida para obter o total de registros (recordsTotal)
        params["length"] = "1"
        resp_check = client.get(api_endpoint, params=params)

        if resp_check.status_code != 200:
            raise Exception(
                f"Erro ao consultar API de rotas: Status"
                f" {resp_check.status_code}"
            )

        data_check = resp_check.json()
        total_records = data_check.get("recordsTotal", 0)

        if total_records == 0:
            raise Exception(
                "Nenhum registro de rota retornado. Verifique a autenticação."
            )

        # 2. Segunda consulta com a quantidade total exata
        params["length"] = str(total_records)
        resp_full = client.get(api_endpoint, params=params)

        if resp_full.status_code == 200:
            full_data = resp_full.json()
            routes_list = full_data.get("data", [])

            with open(output_json_path, "w", encoding="utf-8") as f:
                json.dump(
                    routes_list, f, ensure_ascii=False, indent=4, default=str
                )

            print(
                f"Sucesso! {len(routes_list)} rotas salvas em"
                f" {output_json_path}"
            )
            return str(output_json_path)
        else:
            raise Exception(
                f"Erro ao obter lote completo de rotas: Status"
                f" {resp_full.status_code}"
            )