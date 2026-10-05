# scraping/services/extra_service.py
"""Atendimento extra: ligar uma marca, num dia escolhido, nas lojas de uma rede ou escolhidas.

A marca é ligada na rota AGENDADA do dia do promotor de ROTA FIXA que faz aquela marca na loja,
do mesmo jeito que o reagendamento faz com a rota de hoje.
"""

import html
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
from django.conf import settings
from playwright.sync_api import sync_playwright

from rules_engine.services.frequency_checker import extract_client_group, get_day_index, normalize_text
from scraping.services.calendar_automation import (
    PARALLEL_WORKERS,
    RotasDeHoje,
    activate_brands_in_itinerary,
    find_today_itinerary,
)
from scraping.services.puzzle_session import BROWSER_HEADERS, puzzle_login

logger = logging.getLogger(__name__)

REDES = ["ASSAÍ", "ATACADÃO"]

# Situações apuradas na prévia (iguais aos status de AtendimentoExtraItem)
PRONTO, JA_ATIVO, SEM_PROMOTOR = "PRONTO", "JA_ATIVO", "SEM_PROMOTOR"
SEM_ROTA, FORA_DA_ROTA, ROTA_FINALIZADA, NAO_VERIFICADO = "SEM_ROTA", "FORA_DA_ROTA", "ROTA_FINALIZADA", "NAO_VERIFICADO"


def load_fixed_routes() -> list:
    """Rotas fixas baixadas na última sincronização (media/json_exports/fixed_routes.json)."""
    caminho = Path(settings.MEDIA_ROOT) / "json_exports" / "fixed_routes.json"
    if not caminho.exists():
        return []
    with open(caminho, encoding="utf-8") as f:
        return json.load(f)


def fixed_promoters_by_store(rotas_fixas: list, canonica=normalize_text) -> dict:
    """{loja_norm: {"loja", "rede", "marcas": {marca_canonica: {"nome", "promotores": [(id, nome, dia_idx)]}}}}

    Só lojas do Assaí e do Atacadão. Uma loja + marca pode ter mais de um promotor (dias diferentes).
    """
    lojas = {}
    for r in rotas_fixas:
        loja = html.unescape(r.get("company_fullname") or "")
        rede = extract_client_group(loja)
        marca = html.unescape(r.get("industry_fullname") or "")
        if not rede or not marca or not r.get("pearson_id"):
            continue
        dados = lojas.setdefault(normalize_text(loja), {"loja": loja, "rede": rede, "marcas": {}})
        por_marca = dados["marcas"].setdefault(canonica(marca), {"nome": marca, "promotores": []})
        promotor = (r["pearson_id"], (r.get("pearson_fullname") or "").strip(), get_day_index(r.get("day_of_week") or ""))
        if promotor not in por_marca["promotores"]:
            por_marca["promotores"].append(promotor)
    return lojas


def brands_with_fixed_routes(indice: dict) -> list:
    """Marcas que têm promotor de rota fixa em alguma loja do Assaí/Atacadão, em ordem alfabética."""
    return sorted({dados["nome"] for loja in indice.values() for dados in loja["marcas"].values()})


def stores_for_brand(indice: dict, marca: str, canonica=normalize_text, redes=None) -> list:
    """Lojas onde a marca tem promotor de rota fixa: [{"loja", "rede", "promotores": [nomes]}]."""
    marca_c = canonica(marca)
    lojas = []
    for dados in indice.values():
        if redes and dados["rede"] not in redes:
            continue
        por_marca = dados["marcas"].get(marca_c)
        if por_marca:
            lojas.append({
                "loja": dados["loja"],
                "rede": dados["rede"],
                "promotores": sorted({nome for _, nome, _ in por_marca["promotores"]}),
            })
    return sorted(lojas, key=lambda l: l["loja"])


def _ordenar_candidatos(promotores: list, dia) -> list:
    """Promotores da rota fixa da loja + marca, primeiro os que fazem a loja nesse dia da semana."""
    dia_semana = dia.weekday()
    unicos = {}
    for pid, nome, dia_idx in promotores:
        atual = unicos.get(pid)
        if atual is None or dia_idx == dia_semana:
            unicos[pid] = (pid, nome, dia_idx)
    return sorted(unicos.values(), key=lambda p: p[2] != dia_semana)


def _item(loja, status, log, promotor=None, itinerario=None):
    return {
        "ponto_de_venda": loja,
        "promotor_designado": promotor[1] if promotor else "",
        "promotor_id": promotor[0] if promotor else None,
        "itinerary_id": itinerario["id"] if itinerario else None,
        "status": status,
        "log": log,
    }


def preview_extra(marca: str, dia, lojas: list, canonica=normalize_text, indice: dict | None = None) -> list:
    """SOMENTE LEITURA: situação de cada loja para ligar a marca na rota agendada do dia.

    Faz UM login (o Puzzle mantém uma sessão por usuário) e consulta tudo por HTTP.
    :param lojas: nomes das lojas (como nas rotas fixas)
    :return: [{"ponto_de_venda", "promotor_designado", "promotor_id", "itinerary_id", "status", "log"}]
    """
    indice = indice if indice is not None else fixed_promoters_by_store(load_fixed_routes(), canonica)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_context().new_page()
            puzzle_login(page)
            cookies = {c["name"]: c["value"] for c in page.context.cookies()}
        finally:
            browser.close()

    with httpx.Client(cookies=cookies, headers=BROWSER_HEADERS, timeout=60.0) as client:
        return classify_extra(client, marca, dia, lojas, canonica, indice)


def classify_extra(client, marca: str, dia, lojas: list, canonica, indice: dict) -> list:
    """SOMENTE LEITURA: classifica cada loja com o cliente HTTP já autenticado (ver preview_extra)."""
    marca_c = canonica(marca)
    data_txt = f"{dia:%d/%m/%Y}"

    candidatos_por_loja = {}
    for loja in lojas:
        dados = indice.get(normalize_text(loja), {})
        promotores = dados.get("marcas", {}).get(marca_c, {}).get("promotores", [])
        candidatos_por_loja[loja] = _ordenar_candidatos(promotores, dia)

    promotores = {p[0] for candidatos in candidatos_por_loja.values() for p in candidatos}
    print(f"--- [ATENDIMENTO EXTRA] prévia de {marca} em {data_txt}: {len(lojas)} loja(s), {len(promotores)} promotor(es) ---")

    rotas = RotasDeHoje(client, canonica, dia)
    falhas = {}   # promotor -> erro na consulta (a loja fica "não verificada", não "sem rota")

    def carregar(pid):
        try:
            if rotas.itinerario(pid):
                rotas.marcas_ativas(pid)
        except Exception as e:
            falhas[pid] = str(e)
            logger.warning(f"Não foi possível ler a rota de {data_txt} do promotor {pid}: {e}")

    with ThreadPoolExecutor(max_workers=PARALLEL_WORKERS) as executor:
        list(executor.map(carregar, promotores))

    itens = []
    for loja in lojas:
        candidatos = candidatos_por_loja[loja]
        if not candidatos:
            itens.append(_item(loja, SEM_PROMOTOR, "A marca não tem promotor de rota fixa nesta loja."))
            continue

        loja_norm = normalize_text(loja)
        escolhido = algum_com_rota = falhou = None
        for promotor in candidatos:
            if promotor[0] in falhas:
                falhou = falhou or (promotor, falhas[promotor[0]])
                continue
            itinerario = rotas.itinerario(promotor[0])   # já está no cache
            if itinerario is None:
                continue
            algum_com_rota = algum_com_rota or (promotor, itinerario)
            if loja_norm in itinerario["lojas"]:
                escolhido = (promotor, itinerario)
                break

        if escolhido is None:
            if falhou:
                itens.append(_item(loja, NAO_VERIFICADO, f"Não foi possível ler a rota no Puzzle: {falhou[1]}", falhou[0]))
            elif algum_com_rota:
                promotor, itinerario = algum_com_rota
                itens.append(_item(loja, FORA_DA_ROTA,
                                   f"A loja não está na rota de {data_txt} do promotor (itinerário {itinerario['id']}).",
                                   promotor, itinerario))
            else:
                itens.append(_item(loja, SEM_ROTA, f"O promotor da rota fixa não tem rota agendada em {data_txt}.",
                                   candidatos[0]))
            continue

        promotor, itinerario = escolhido
        if not itinerario["editavel"]:
            itens.append(_item(loja, ROTA_FINALIZADA,
                               f"A rota de {data_txt} já está finalizada (itinerário {itinerario['id']}).",
                               promotor, itinerario))
            continue
        ativos = rotas.marcas_ativas(promotor[0]) or {}
        if marca_c in ativos.get(loja_norm, set()):
            itens.append(_item(loja, JA_ATIVO,
                               f"A marca já está ligada na rota de {data_txt} (itinerário {itinerario['id']}).",
                               promotor, itinerario))
        else:
            itens.append(_item(loja, PRONTO,
                               f"Marca desligada na rota de {data_txt} (itinerário {itinerario['id']}); será ligada.",
                               promotor, itinerario))
    return itens


def execute_extra(itens: list, marca: str, dia, canonica=normalize_text) -> dict:
    """Liga a marca na rota agendada do dia, uma vez por promotor. SEM acesso ao banco.

    Confere de novo o itinerário do dia (a prévia pode ter ficado velha) antes de alterar.
    :param itens: [{"id", "ponto_de_venda", "promotor_id"}] das lojas PRONTAS da prévia
    :return: {id: {"status": "success" | "already_active" | "error", "message", "itinerary_id"}}
    """
    data_txt = f"{dia:%d/%m/%Y}"
    resultados = {}
    por_promotor = {}
    for item in itens:
        por_promotor.setdefault(item["promotor_id"], []).append({**item, "industria": marca})

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=settings.PUZZLE_HEADLESS)
        context = browser.new_context()
        try:
            puzzle_login(context.new_page())
            total = len(por_promotor)
            print(f"--- [ATENDIMENTO EXTRA] ligando {marca} em {data_txt}: {len(itens)} loja(s), {total} promotor(es) ---")
            for n, (pid, itens_promotor) in enumerate(por_promotor.items(), start=1):
                print(f"   [{n}/{total}] promotor {pid}: {len(itens_promotor)} loja(s)")
                try:
                    itinerario = find_today_itinerary(context, pid, dia)
                except Exception as e:
                    for i in itens_promotor:
                        resultados[i["id"]] = {"status": "error", "message": str(e), "itinerary_id": None}
                    continue
                if not itinerario or not itinerario["editavel"]:
                    motivo = "não tem mais rota agendada" if not itinerario else "teve a rota finalizada"
                    for i in itens_promotor:
                        resultados[i["id"]] = {
                            "status": "error",
                            "message": f"O promotor {motivo} em {data_txt}.",
                            "itinerary_id": itinerario and itinerario["id"],
                        }
                    continue

                na_rota = [i for i in itens_promotor if normalize_text(i["ponto_de_venda"]) in itinerario["lojas"]]
                for i in itens_promotor:
                    if i not in na_rota:
                        resultados[i["id"]] = {
                            "status": "error",
                            "message": f"A loja saiu da rota de {data_txt} (itinerário {itinerario['id']}).",
                            "itinerary_id": itinerario["id"],
                        }
                if na_rota:
                    resultados.update(
                        activate_brands_in_itinerary(context, itinerario["id"], na_rota, canonica, f"de {data_txt}")
                    )
        except Exception as e:
            for item in itens:
                resultados.setdefault(item["id"], {"status": "error", "message": f"Execução interrompida: {e}",
                                                   "itinerary_id": None})
        finally:
            browser.close()
    return resultados
