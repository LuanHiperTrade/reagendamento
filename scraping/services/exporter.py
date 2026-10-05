# scraping/services/exporter.py

import html as htmlmod
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

from django.conf import settings
from playwright.sync_api import sync_playwright

from rules_engine.services.frequency_checker import normalize_text
from scraping.services.puzzle_session import BASE_URL, XHR_HEADERS, puzzle_login
from scraping.services.routes_api import fetch_fixed_routes
from scraping.utils.converters import convert_excel_to_json

# Quanto tempo esperar o Puzzle gerar o Indicador de Contratos depois de pedir a exportação
ESPERA_EXPORTACAO_S = 300
INTERVALO_CONSULTA_S = 5


def parse_notifications(corpo: str) -> list:
    """Lê o HTML de /notification/list: [{"id", "texto", "quando", "link"}].

    Cada notificação tem um id (uuid), o texto (status + título, ex.: "Concluído! Geração Relatorio
    de Indicador Contratos - ..."), a data/hora e, quando o arquivo está pronto, o link do S3.
    """
    notificacoes = []
    for bloco in re.split(r'(?=<li[^>]*class="[^"]*notification_item)', corpo)[1:]:
        uid = re.search(r'id="([^"]+)"', bloco)
        texto = re.search(r'<p class="mb-0">(.*?)<small', bloco, re.S)
        quando = re.search(r"(\d{2}/\d{2}/\d{4} \d{2}:\d{2})", bloco)
        link = re.search(r'<a href="(https?://[^"]+)"', bloco)
        notificacoes.append({
            "id": uid.group(1) if uid else "",
            "texto": " ".join(re.sub(r"<[^>]+>", " ", htmlmod.unescape(texto.group(1))).split()) if texto else "",
            "quando": datetime.strptime(quando.group(1), "%d/%m/%Y %H:%M") if quando else None,
            "link": htmlmod.unescape(link.group(1)) if link else None,
        })
    return notificacoes


def is_contracts_report(notificacao: dict) -> bool:
    """A notificação é do Indicador de Contratos (e não de rotas fixas, geração de rotas etc.)?"""
    texto = normalize_text(notificacao["texto"])
    return "INDICADOR" in texto and "CONTRATO" in texto


def pick_new_contracts_report(notificacoes: list, ids_anteriores: set, pedido_em: datetime) -> dict | None:
    """Notificação NOVA (criada depois do pedido) do Indicador de Contratos, já com o link do arquivo.

    A lista tem exportações de outras pessoas e de outros relatórios (o usuário do robô é
    compartilhado); pegar "o primeiro link" já baixou o relatório de rotas fixas de um promotor.
    """
    limite = pedido_em.replace(second=0, microsecond=0) - timedelta(minutes=1)   # o Puzzle mostra só minutos
    for n in notificacoes:
        if n["id"] in ids_anteriores or not n["link"] or not is_contracts_report(n):
            continue
        if n["quando"] and n["quando"] < limite:
            continue
        return n
    return None


def _listar_notificacoes(page) -> list:
    resp = page.context.request.get(f"{BASE_URL}/notification/list", headers=XHR_HEADERS)
    return parse_notifications(resp.json().get("html", "")) if resp.ok else []


def run_full_scraping_and_conversion() -> dict:
    """Executa o fluxo completo dos Passos 1 ao 6:

    1. Login no Puzzle via Playwright
    2. Solicitação da exportação do Indicador de Contratos
    3. Espera a notificação NOVA desse relatório e baixa a planilha (S3)
    4. Conversão da planilha (.xlsx) para JSON
    5. Extração dinâmica de 100% das Rotas Fixas via API
    """
    downloads_dir = Path(settings.MEDIA_ROOT) / "downloads"
    downloads_dir.mkdir(parents=True, exist_ok=True)
    excel_path = downloads_dir / "Indicador_de_contratos.xlsx"

    print("--- [SCRAPING] Iniciando coleta de dados do Puzzle ---")

    with sync_playwright() as p:
        # Modo headless True para execução transparente
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        try:
            # 1. Login
            print("1. Efetuando Login...")
            puzzle_login(page)

            # 2. Pede a exportação, anotando antes as notificações que já existem
            print("2. Acessando painel de exportação...")
            page.goto(f"{BASE_URL}/dashboards/indicators-contracts", wait_until="domcontentloaded", timeout=60000)
            export_btn = page.locator("#btn-export-all")
            export_btn.wait_for(state="visible", timeout=60000)

            ids_anteriores = {n["id"] for n in _listar_notificacoes(page)}
            pedido_em = datetime.now()
            with page.expect_response(lambda r: "/indicators-contracts/export/" in r.url, timeout=60000) as resp:
                export_btn.click()
            print(f"   Exportação solicitada (Puzzle respondeu {resp.value.status}).")

            # 3. Espera a notificação NOVA do Indicador de Contratos com o link do arquivo
            print("3. Aguardando o Puzzle gerar o Indicador de Contratos...")
            notificacao = None
            limite = time.time() + ESPERA_EXPORTACAO_S
            while time.time() < limite:
                notificacao = pick_new_contracts_report(_listar_notificacoes(page), ids_anteriores, pedido_em)
                if notificacao:
                    break
                time.sleep(INTERVALO_CONSULTA_S)

            planilha_reutilizada = False
            if notificacao:
                print(f"4. Baixando planilha .xlsx ({notificacao['texto'][:80]} - {notificacao['quando']:%H:%M})...")
                response = page.request.get(notificacao["link"])
                if not response.ok:
                    raise Exception(f"Falha ao baixar arquivo do S3 (Status {response.status})")
                with open(excel_path, "wb") as f:
                    f.write(response.body())
                print(f"   Planilha salva em: {excel_path}")
            else:
                # Sem notificação nova a tempo: reaproveita a última planilha (a validação barra semana errada)
                if not excel_path.exists():
                    raise Exception(
                        f"O Puzzle não gerou o Indicador de Contratos em {ESPERA_EXPORTACAO_S // 60} minutos."
                    )
                print("   O Puzzle não gerou o arquivo a tempo: reutilizando a última planilha baixada.")
                planilha_reutilizada = True

            # 5. Converter a planilha para JSON
            print("5. Convertendo .xlsx para .json...")
            json_reagendamentos_path = convert_excel_to_json(str(excel_path))

            # 6. Extrair Rotas Fixas via API usando a sessão ativa
            print("6. Extraindo Rotas Fixas via API...")
            cookies = context.cookies()
            json_rotas_path = fetch_fixed_routes(cookies)

            return {
                "status": "success",
                "reagendamentos_json": json_reagendamentos_path,
                "rotas_json": json_rotas_path,
                "planilha_reutilizada": planilha_reutilizada,
                "planilha_modificada_em": excel_path.stat().st_mtime,
            }

        except Exception as e:
            print(f"Erro no processo de scraping: {e}")
            raise e

        finally:
            browser.close()
