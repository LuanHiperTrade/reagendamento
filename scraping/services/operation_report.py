# scraping/services/operation_report.py

import html as htmlmod
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import httpx

from rules_engine.services.frequency_checker import normalize_text
from scraping.services.puzzle_session import BASE_URL, XHR_HEADERS, puzzle_get


def parse_operation_cards(body: str) -> list:
    """Lê os cards do relatório de operação: um por promotor, com lojas e tarefas do dia.

    Cada tarefa aponta para a atividade (/survey_schedules/edit/{id}), que tem a indústria cadastrada.
    :return: [{"pearson_id", "nome", "lojas": [loja, ...], "tarefas": [(loja, titulo, id_atividade), ...]}]
    """
    cards = []
    for bloco in re.split(r'<div class="col-md-6 col-lg-6 col-xl-4">', body)[1:]:
        pid = re.search(r"itineraries/calendar/(\d+)", bloco)
        if not pid:
            continue
        nome = re.search(r'<h5 class="card-title text-center">\s*([^<]+?)\s*<div', bloco)

        roteiro = re.search(r'id="collapse\d+">(.*?)</table>', bloco, re.S)
        lojas = []
        if roteiro:
            lojas = [
                htmlmod.unescape(loja)
                for loja in re.findall(r'show-html/\d+"[^>]*title="([^"]+)"', roteiro.group(1))
            ]

        tabela_tarefas = re.search(r'id="collapse\d+_tasks">(.*?)</table>', bloco, re.S)
        tarefas = []
        if tabela_tarefas:
            for linha in re.findall(r"<tr>(.*?)</tr>", tabela_tarefas.group(1), re.S):
                loja = re.search(r'show-html/\d+"[^>]*title="([^"]+)"', linha)
                titulo = re.search(r'survey_schedules/edit/(\d+)"[^>]*title="([^"]+)"', linha)
                if loja and titulo:
                    tarefas.append((
                        htmlmod.unescape(loja.group(1)),
                        htmlmod.unescape(titulo.group(2)),
                        int(titulo.group(1)),
                    ))

        cards.append({
            "pearson_id": int(pid.group(1)),
            "nome": nome.group(1).strip() if nome else "",
            "lojas": lojas,
            "tarefas": tarefas,
        })
    return cards


def fetch_operation_today(client: httpx.Client, workers: int = 8) -> dict:
    """Quem está em cada loja HOJE e com quais tarefas, segundo /reports/promoters/operation.

    :return: {"promotores": {loja_norm: {pearson_id: nome}},
              "tarefas": {loja_norm: [(pearson_id, nome, titulo_da_tarefa, id_atividade), ...]}}
    """
    hoje = datetime.now().strftime("%d/%m/%Y")

    def pagina(n):
        params = {
            "page": str(n), "with_promoter_ids": "", "with_regional_ids": "", "by_day": hoje,
            "with_supervisor_ids": "", "with_city_ids": "", "with_profiles": "",
        }
        return puzzle_get(
            client, f"{BASE_URL}/reports/promoters/get-data", params=params, headers=XHR_HEADERS
        ).json()["html"]

    primeira = pagina(1)
    total = max([int(n) for n in re.findall(r'page_select[^"]*" id="(\d+)"', primeira)] or [1])
    with ThreadPoolExecutor(max_workers=workers) as executor:
        corpos = [primeira, *executor.map(pagina, range(2, total + 1))]

    promotores, tarefas = {}, {}
    for corpo in corpos:
        for card in parse_operation_cards(corpo):
            pid, nome = card["pearson_id"], card["nome"]
            for loja in card["lojas"]:
                promotores.setdefault(normalize_text(loja), {})[pid] = nome
            for loja, titulo, id_atividade in card["tarefas"]:
                tarefas.setdefault(normalize_text(loja), []).append((pid, nome, titulo, id_atividade))
    return {"promotores": promotores, "tarefas": tarefas}


def fetch_activity_industry(client: httpx.Client, id_atividade: int) -> str:
    """Indústria cadastrada na atividade (/survey_schedules/edit/{id}); "" se não houver. SOMENTE LEITURA."""
    html = puzzle_get(client, f"{BASE_URL}/survey_schedules/edit/{id_atividade}").text
    select = re.search(r'<select[^>]*name="industry_id"[^>]*>(.*?)</select>', html, re.S)
    if not select:
        return ""
    opcao = re.search(r'<option[^>]*value="\d+"[^>]*selected[^>]*>([^<]*)</option>', select.group(1))
    return htmlmod.unescape(opcao.group(1)).strip() if opcao else ""


def is_execution_task(titulo: str) -> bool:
    """Só tarefas de EXECUÇÃO comprovam o atendimento da marca na loja.

    Título terminando em "(EXECUÇÃO)", sem diferenciar maiúsculas/acentos/espaços:
    "FOTOS(Execução)", "ANTES E DEPOIS ((EXECUÇÃO)". "GIROS", "RUPTURA" etc. não contam.
    """
    return bool(re.search(r"\(\s*EXECUCAO\s*\)\s*$", _sem_acento(titulo or "").upper()))


def _sem_acento(texto: str) -> str:
    return unicodedata.normalize("NFD", texto).encode("ascii", "ignore").decode("ascii")


def _compacto(texto: str) -> str:
    return normalize_text(texto).replace(" ", "")


def build_brand_matcher(marcas_conhecidas, equivalentes=None):
    """Devolve uma função titulo_da_tarefa -> marca (normalizada) ou None.

    Regras (ignorando acentos e espaços: "AGRO FOODS" = "AGROFOODS"):
    1. "MARCA - TAREFA": vale o nome EXATO antes do " - " (marca ou nome equivalente).
    2. Sem o separador (ex.: "ARCHOTE FOTOS (EXECUÇÃO)"): vale o começo do título, pela marca
       mais longa. Se essa marca for prefixo de outra (família, ex.: MONRÉVE x MONRÉVE MP,
       PRATS x PRATS COMPARTILHADO), o título é ambíguo e NÃO conta — "MONRÉVE BATATA PALHA"
       só conta se for cadastrado como marca equivalente.

    :param equivalentes: {nome alternativo: marca} (ex.: {"TUCS/FAZENDINHA": "BELA VISTA"})
    """
    nomes = {_compacto(m): normalize_text(m) for m in marcas_conhecidas if m}
    nomes.update({_compacto(alt): normalize_text(marca) for alt, marca in (equivalentes or {}).items()})
    nomes.pop("", None)

    # Nome que é começo de outro nome de OUTRA marca: família, ambígua sem o separador
    # (MONRÉVE x MONRÉVE MP). "LIBERTY" x "LIBERTY PLAST" são a mesma marca: não é ambíguo.
    ambiguos = {
        a for a in nomes
        if any(b != a and b.startswith(a) and nomes[b] != nomes[a] for b in nomes)
    }
    por_tamanho = sorted(nomes, key=len, reverse=True)

    def marca_da_tarefa(titulo: str):
        cabeca = _compacto(re.split(r"\s+[-–]\s+", titulo or "", maxsplit=1)[0])
        if cabeca in nomes:
            return nomes[cabeca]
        compacto = _compacto(titulo)
        for nome in por_tamanho:
            if compacto.startswith(nome):
                return None if nome in ambiguos else nomes[nome]
        return None

    return marca_da_tarefa
