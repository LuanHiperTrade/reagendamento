# scraping/services/calendar_automation.py

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import httpx
from playwright.sync_api import BrowserContext, Page, expect, sync_playwright

from rules_engine.services.brands import make_canonical
from rules_engine.services.frequency_checker import normalize_text
from scraping.services.operation_report import (
    build_brand_matcher,
    fetch_activity_industry,
    fetch_operation_today,
    is_execution_task,
)
from scraping.services.puzzle_session import (
    BASE_URL,
    BROWSER_HEADERS,
    XHR_HEADERS,
    puzzle_get,
    puzzle_login,
)

logger = logging.getLogger(__name__)

# Quantos promotores são consultados ao mesmo tempo na sincronização
PARALLEL_WORKERS = 8

# Ligar marca na rota: 1ª tentativa + 2 novas tentativas se o salvamento não for confirmado
TENTATIVAS_SALVAR = 3

# Situações da rota de hoje apuradas na sincronização (a view converte em status)
SITUACAO_AGENDADA = "agendada_hoje"
SITUACAO_COBERTA = "coberta_por_outro"
SITUACAO_A_REAGENDAR = "a_reagendar"
SITUACAO_FINALIZADA = "rota_finalizada"
SITUACAO_SEM_ROTA = "sem_rota_hoje"
SITUACAO_FORA_DA_ROTA = "fora_da_rota"
SITUACAO_NAO_VERIFICADA = "nao_verificada"


def _today_params(dia=None) -> dict:
    """Parâmetros do feed do calendário para um dia (padrão: hoje)."""
    dia = dia or datetime.now().date()
    return {
        "start": f"{dia:%Y-%m-%d}T00:00:00-03:00",
        "end": f"{dia + timedelta(days=1):%Y-%m-%d}T00:00:00-03:00",
    }


def parse_today_itinerary(eventos: list, dia=None) -> dict | None:
    """Extrai do feed do calendário a rota do dia (padrão: HOJE).

    Cada evento do feed é a rota de um promotor em um dia; o 'id' do evento é o
    ID usado em /itineraries/edit/{id}. Rotas finalizadas não têm link de edição.
    """
    dia = dia or datetime.now().date()
    if not isinstance(eventos, list):
        return None
    for evento in eventos:
        title = evento.get("title") or ""
        if f"Rota dia {dia:%d/%m/%Y}" not in title:
            continue
        return {
            "id": evento["id"],
            "editavel": f"/itineraries/edit/{evento['id']}" in title,
            # As lojas vêm em <li title="NOME DA LOJA">; os botões de ação usam aspas simples
            "lojas": [normalize_text(loja) for loja in re.findall(r'<li[^>]*title="([^"]+)"', title)],
        }
    return None


def find_today_itinerary(context: BrowserContext, promoter_id: int, dia=None) -> dict | None:
    """Consulta (via sessão do Playwright) o feed do calendário e retorna a rota do dia (padrão: HOJE)."""
    resp = context.request.get(
        f"{BASE_URL}/itineraries/calendar/list/{promoter_id}",
        params=_today_params(dia),
        headers=XHR_HEADERS,
    )
    if not resp.ok:
        raise RuntimeError(
            f"Falha ao consultar o calendário do promotor {promoter_id} (Status {resp.status})."
        )
    return parse_today_itinerary(resp.json(), dia)


def parse_active_contracts(html: str, canonica=normalize_text) -> dict:
    """Lê as marcas ATIVAS por loja a partir do JSON embutido na tela de edição.

    A página traz cada loja em ToDoKm.Routes.SalesPoint2Li({...}) e a lista "contracts"
    contém somente os contratos ligados na rota (as marcas desligadas o JavaScript
    acrescenta depois). Validado contra a tela renderizada em 7 rotas reais.
    :return: {loja_normalizada: {marca_normalizada, ...}}
    """
    decoder = json.JSONDecoder()
    lojas = {}
    for m in re.finditer(r"SalesPoint2Li\(\s*(?=\{)", html):
        loja, _ = decoder.raw_decode(html, m.end())
        lojas[normalize_text(loja.get("name"))] = {
            canonica(c.get("industry"))
            for c in loja.get("contracts", [])
            if not c.get("disabled_route")
        }
    return lojas


class RotasDeHoje:
    """Cache, durante uma sincronização, da rota de hoje de cada promotor (SOMENTE LEITURA).

    Cada promotor tem o calendário e a tela de edição baixados no máximo uma vez, mesmo
    que apareça como designado de uma proposta e como "outro promotor" em outra.
    """

    def __init__(self, client: httpx.Client, canonica=normalize_text, dia=None):
        self.client = client
        self.canonica = canonica      # nome da marca -> marca normalizada (com equivalências)
        self.dia = dia                # None = hoje (atendimento extra usa outras datas)
        self._itinerarios = {}
        self._ativos = {}

    def itinerario(self, promoter_id: int) -> dict | None:
        if promoter_id not in self._itinerarios:
            resp = puzzle_get(
                self.client,
                f"{BASE_URL}/itineraries/calendar/list/{promoter_id}",
                params=_today_params(self.dia),
                headers=XHR_HEADERS,
            )
            self._itinerarios[promoter_id] = parse_today_itinerary(resp.json(), self.dia)
        return self._itinerarios[promoter_id]

    def marcas_ativas(self, promoter_id: int) -> dict | None:
        """{loja: {marcas ativas}} da rota de hoje; None se não há rota ou ela está finalizada
        (rota finalizada redireciona e não mostra mais as marcas)."""
        itinerario = self.itinerario(promoter_id)
        if not itinerario or not itinerario["editavel"]:
            return None
        if promoter_id not in self._ativos:
            html = puzzle_get(self.client, f"{BASE_URL}/itineraries/edit/{itinerario['id']}").text
            self._ativos[promoter_id] = parse_active_contracts(html, self.canonica)
        return self._ativos[promoter_id]


def _classify_promoter(rotas: RotasDeHoje, promoter_id: int, itens: list) -> dict:
    """SOMENTE LEITURA: apura a situação de cada proposta do promotor na rota de hoje.

    Uma consulta ao calendário por promotor; a tela de edição só é baixada se a rota
    estiver aberta e alguma loja das propostas estiver nela.
    :return: {id(item): (situacao, log)}
    """
    itinerario = rotas.itinerario(promoter_id)

    if not itinerario:
        return {id(i): (SITUACAO_SEM_ROTA, "O promotor não tem rota hoje no Puzzle.") for i in itens}

    itinerary_id = itinerario["id"]
    if not itinerario["editavel"]:
        return {
            id(i): (SITUACAO_FINALIZADA, f"A rota de hoje já está finalizada (itinerário {itinerary_id}).")
            for i in itens
        }

    resultado = {}
    na_rota = []
    for item in itens:
        if normalize_text(item["cliente"]) in itinerario["lojas"]:
            na_rota.append(item)
        else:
            resultado[id(item)] = (
                SITUACAO_FORA_DA_ROTA,
                f"A loja não está na rota de hoje do promotor (itinerário {itinerary_id}).",
            )

    if not na_rota:
        return resultado

    ativos = rotas.marcas_ativas(promoter_id)
    for item in na_rota:
        marcas_ativas = ativos.get(normalize_text(item["cliente"]), set())
        if rotas.canonica(item["industria"]) in marcas_ativas:
            resultado[id(item)] = (
                SITUACAO_AGENDADA,
                f"Marca já ativa na rota de hoje do promotor (itinerário {itinerary_id}).",
            )
        else:
            resultado[id(item)] = (
                SITUACAO_A_REAGENDAR,
                f"Marca desligada na rota de hoje (itinerário {itinerary_id}); pronta para reagendar.",
            )
    return resultado


def _resolve_activity_industries(rotas: RotasDeHoje, titulos: dict, atividades: dict, lidas: dict) -> None:
    """Lê do Puzzle (em paralelo) a indústria das atividades que ainda não estão no cache.

    :param titulos: {id da atividade: título da tarefa}
    :param lidas: recebe {id: (título, indústria)} das atividades lidas agora
    """
    faltam = sorted(i for i in titulos if i not in atividades)
    if not faltam:
        return
    print(f"   Lendo a indústria de {len(faltam)} atividade(s) no cadastro do Puzzle...")

    def ler(id_atividade):
        try:
            return id_atividade, fetch_activity_industry(rotas.client, id_atividade)
        except Exception as e:
            logger.warning(f"Não foi possível ler a atividade {id_atividade}: {e}")
            return id_atividade, None

    with ThreadPoolExecutor(max_workers=PARALLEL_WORKERS) as executor:
        for id_atividade, industria in executor.map(ler, faltam):
            if industria is not None:
                atividades[id_atividade] = industria
                lidas[id_atividade] = (titulos[id_atividade], industria)


def check_coverage_by_others(
    rotas: RotasDeHoje, propostas: list, operacao: dict, marca_da_tarefa,
    atividades: dict | None = None, lidas: dict | None = None,
) -> None:
    """SOMENTE LEITURA: marca como 'coberta_por_outro' a proposta cuja marca está, hoje, com
    OUTRO promotor na mesma loja. Duas formas de confirmar:

    1. A marca está ligada na rota de hoje do outro promotor (rota aberta).
    2. O outro promotor tem hoje, nessa loja, uma tarefa de EXECUÇÃO da marca (título terminando
       em "(EXECUÇÃO)"; GIROS, RUPTURA, PESQUISA DE PREÇO etc. não contam). A marca da tarefa vem da
       indústria cadastrada na atividade do Puzzle (ex.: "LOBIT'S - SHARE" = MILHO DE OURO); o
       título só é usado se a atividade não tiver indústria. Vale mesmo com a rota finalizada.

    Se o outro promotor esteve na loja, mas não dá para confirmar a marca, apenas anota no log.
    :param operacao: retorno de fetch_operation_today (promotores e tarefas por loja)
    :param marca_da_tarefa: função de build_brand_matcher (título da tarefa -> marca)
    :param atividades: cache {id da atividade: indústria}; recebe as atividades lidas agora
    :param lidas: recebe {id: (título, indústria)} das atividades lidas do Puzzle agora (para gravar)
    """
    atividades = {} if atividades is None else atividades
    lidas = {} if lidas is None else lidas
    promotores_por_loja = operacao["promotores"]
    # Só tarefas de EXECUÇÃO comprovam o atendimento da marca (GIROS, RUPTURA etc. não contam)
    tarefas_por_loja = {
        loja: [t for t in tarefas if is_execution_task(t[2])]
        for loja, tarefas in operacao["tarefas"].items()
    }

    # Atividades das tarefas de OUTROS promotores nas lojas das propostas
    titulos = {
        id_atividade: titulo
        for p in propostas
        for pid, _, titulo, id_atividade in tarefas_por_loja.get(normalize_text(p["cliente"]), [])
        if pid != p.get("promotor_id")
    }
    _resolve_activity_industries(rotas, titulos, atividades, lidas)

    def marca_tarefa(titulo, id_atividade):
        industria = atividades.get(id_atividade)
        return rotas.canonica(industria) if industria else marca_da_tarefa(titulo)
    outros = {
        pid
        for p in propostas
        for pid in promotores_por_loja.get(normalize_text(p["cliente"]), {})
        if pid != p.get("promotor_id")
    }

    if outros:
        print(f"8. Verificando cobertura por outros promotores ({len(outros)} rota(s))...")
        with ThreadPoolExecutor(max_workers=PARALLEL_WORKERS) as executor:
            list(executor.map(_safe_marcas_ativas, [rotas] * len(outros), outros))

    for p in propostas:
        loja, marca = normalize_text(p["cliente"]), rotas.canonica(p["industria"])
        designado = p.get("promotor_id")

        # 1. Marca ligada na rota de outro promotor
        finalizadas = []
        coberta = False
        for pid, nome in promotores_por_loja.get(loja, {}).items():
            if pid == designado:
                continue
            ativos = _safe_marcas_ativas(rotas, pid)
            if ativos is not None and marca in ativos.get(loja, set()):
                itinerario = rotas.itinerario(pid)
                p["situacao"] = SITUACAO_COBERTA
                p["log"] = (
                    f"Coberta por {nome} (promotor {pid}): marca ligada na rota de hoje dele"
                    f" nesta loja (itinerário {itinerario['id']})."
                )
                coberta = True
                break
            if ativos is None and _safe_itinerario(rotas, pid):
                finalizadas.append(nome)
        if coberta:
            continue

        # 2. Tarefa da marca na loja com outro promotor hoje
        for pid, nome, titulo, id_atividade in tarefas_por_loja.get(loja, []):
            if pid != designado and marca_tarefa(titulo, id_atividade) == marca:
                p["situacao"] = SITUACAO_COBERTA
                p["log"] = f"Coberta por {nome} (promotor {pid}): tarefa \"{titulo}\" hoje nesta loja."
                coberta = True
                break

        if not coberta:
            if finalizadas:
                nota = (
                    f" Obs.: {', '.join(finalizadas)} esteve(estiveram) na loja hoje (rota finalizada),"
                    " mas não foi possível confirmar a marca."
                )
                p["log"] = (p.get("log") or "").strip() + nota


def _safe_marcas_ativas(rotas: RotasDeHoje, promoter_id: int):
    try:
        return rotas.marcas_ativas(promoter_id)
    except Exception as e:
        logger.warning(f"Não foi possível ler a rota de hoje do promotor {promoter_id}: {e}")
        return None


def _safe_itinerario(rotas: RotasDeHoje, promoter_id: int):
    try:
        return rotas.itinerario(promoter_id)
    except Exception:
        return None


def mark_already_scheduled_today(
    propostas: list, marcas_conhecidas=(), equivalentes=None, atividades=None, atividades_lidas=None,
) -> list:
    """Apura (somente leitura) a situação de cada proposta na rota de hoje.

    1. Rota do promotor designado: já agendada, desligada, finalizada, fora da rota, sem rota.
    2. Cobertura: outro promotor na loja hoje com a marca ligada na rota ou com tarefa da marca.

    Adiciona 'situacao' e 'log' às propostas. Propostas que já chegam com 'situacao' (contrato
    novo, justificada) não são consultadas. Faz UM login pelo Playwright (o Puzzle mantém uma
    sessão por usuário) e consulta tudo em paralelo por HTTP com os cookies dessa sessão.
    Falhas de consulta não bloqueiam a sincronização: a proposta fica 'nao_verificada'.

    :param marcas_conhecidas: nomes de marcas (planilha + rotas fixas) para ler a marca das tarefas
    :param equivalentes: {nome alternativo: marca} (ex.: {"TUCS/FAZENDINHA": "BELA VISTA"})
    :param atividades: cache {id da atividade: indústria} (ver load_activity_industries)
    :param atividades_lidas: recebe {id: (título, indústria)} das atividades lidas agora, para gravar
    """
    verificar = [item for item in propostas if not item.get("situacao")]
    por_promotor = {}
    for item in verificar:
        item["situacao"] = SITUACAO_NAO_VERIFICADA
        if item.get("promotor_id"):
            por_promotor.setdefault(item["promotor_id"], []).append(item)

    if not verificar:
        return propostas

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context()
            page = context.new_page()
            puzzle_login(page)
            cookies = {c["name"]: c["value"] for c in context.cookies()}
        finally:
            browser.close()

    total = len(por_promotor)
    print(f"7. Verificando a rota de hoje de {total} promotores ({PARALLEL_WORKERS} em paralelo)...")

    with httpx.Client(cookies=cookies, headers=BROWSER_HEADERS, timeout=60.0) as client:
        rotas = RotasDeHoje(client, make_canonical(equivalentes))

        with ThreadPoolExecutor(max_workers=PARALLEL_WORKERS) as executor:
            futures = {
                executor.submit(_classify_promoter, rotas, promoter_id, itens): (promoter_id, itens)
                for promoter_id, itens in por_promotor.items()
            }
            for n, future in enumerate(as_completed(futures), start=1):
                promoter_id, itens = futures[future]
                try:
                    situacoes = future.result()
                except Exception as e:
                    logger.warning(f"Não foi possível verificar a rota de hoje do promotor {promoter_id}: {e}")
                    situacoes = {
                        id(i): (SITUACAO_NAO_VERIFICADA, f"Não foi possível verificar a rota de hoje: {e}")
                        for i in itens
                    }
                for item in itens:
                    anterior = item.get("log")   # ex.: "Contrato novo desde ..." vindo do motor
                    item["situacao"], item["log"] = situacoes[id(item)]
                    if anterior:
                        item["log"] = f"{item['log']} {anterior}"
                print(f"   [{n}/{total}] promotor {promoter_id}: {len(itens)} proposta(s)")

        # Cobertura por outro promotor na mesma loja (inclui propostas sem promotor designado)
        candidatas = [p for p in verificar if p.get("situacao") != SITUACAO_AGENDADA]
        if candidatas:
            try:
                print("   Consultando o relatório de operação (quem está em cada loja e tarefas de hoje)...")
                operacao = fetch_operation_today(client, PARALLEL_WORKERS)
                marcas = {*marcas_conhecidas, *(p.get("industria") for p in propostas)}
                check_coverage_by_others(
                    rotas, candidatas, operacao, build_brand_matcher(marcas, equivalentes),
                    atividades, atividades_lidas,
                )
            except Exception as e:
                logger.warning(f"Não foi possível verificar cobertura por outros promotores: {e}")
                print(f"   Aviso: cobertura por outros promotores não verificada ({e})")

    return propostas


def _find_contract(page: Page, store_name: str, target_brand: str, canonica=normalize_text):
    """Localiza, na tela de edição já aberta, o contrato da marca dentro da loja.

    :param canonica: nome da marca -> marca normalizada com equivalências (a tela mostra
        "LIBERTY" para a LIBERTY PLAST da planilha)
    :return: (contrato, motivo_erro) — contrato é o <small id="contract_ID"> ou None
    """
    loja_norm = normalize_text(store_name)
    brand_norm = canonica(target_brand)

    # A 1ª linha do conteúdo de cada li.dd-item é o nome da loja
    store_li = None
    for li in page.locator("li.dd-item").all():
        nome_loja = li.locator("div.dd3-content").inner_text().split("\n")[0]
        if normalize_text(nome_loja) == loja_norm:
            store_li = li
            break

    if store_li is None:
        return None, f"Loja '{store_name}' não encontrada na tela de edição."

    # Comparação exata: "MONRÉVE" ≠ "MONRÉVE MP"
    for small in store_li.locator("small[id^='contract_']").all():
        marca = small.locator("li").first.inner_text().strip()
        if canonica(marca) == brand_norm:
            return small, None

    return None, f"Marca '{target_brand}' não tem contrato na loja '{store_name}'."


def _is_contract_active(contract) -> bool:
    """Contrato ativo = botão reEnable escondido (d-none)."""
    return "d-none" in (contract.locator("a.reEnable").get_attribute("class") or "")


def apply_reschedules_for_promoter(
    context: BrowserContext, promoter_id: int, itens: list, canonica=normalize_text,
) -> dict:
    """Liga, na rota de HOJE do promotor, todas as marcas das propostas e salva UMA vez.

    :param context: BrowserContext ativo do Playwright (já autenticado)
    :param itens: [{"id", "industria", "ponto_de_venda"}] do mesmo promotor
    :param canonica: nome da marca -> marca normalizada com equivalências (make_canonical)
    :return: {id: {"status": "success" | "already_active" | "error", "message", "itinerary_id"}}
    """
    hoje = datetime.now().strftime("%d/%m/%Y")

    def para_todos(status, message, itinerary_id=None):
        return {
            i["id"]: {"status": status, "message": message, "itinerary_id": itinerary_id}
            for i in itens
        }

    try:
        itinerario = find_today_itinerary(context, promoter_id)
    except Exception as e:
        logger.error(f"Erro ao consultar calendário do promotor {promoter_id}: {e}")
        return para_todos("error", str(e))

    if not itinerario:
        return para_todos("error", f"O promotor {promoter_id} não tem rota no dia {hoje}.")

    itinerary_id = itinerario["id"]
    if not itinerario["editavel"]:
        return para_todos("error", f"A rota de hoje (itinerário {itinerary_id}) já está finalizada.", itinerary_id)

    resultados = {}
    na_rota = []
    for item in itens:
        if normalize_text(item["ponto_de_venda"]) in itinerario["lojas"]:
            na_rota.append(item)
        else:
            resultados[item["id"]] = {
                "status": "error",
                "message": (
                    f"A loja '{item['ponto_de_venda']}' não está na rota de hoje do promotor"
                    f" {promoter_id} (itinerário {itinerary_id})."
                ),
                "itinerary_id": itinerary_id,
            }

    if not na_rota:
        return resultados

    resultados.update(activate_brands_in_itinerary(context, itinerary_id, na_rota, canonica, "de hoje"))
    return resultados


def _salvar_rota(page: Page) -> str | None:
    """Clica em Salvar e ESPERA o Puzzle responder à gravação (/itineraries/update/...).

    Sem essa espera o robô ia direto para a conferência e podia interromper a gravação.
    :return: aviso do Puzzle (status de erro ou mensagem de erro na tela) ou None
    """
    try:
        with page.expect_response(
            lambda r: "/itineraries/update/" in r.url and r.request.method == "POST", timeout=60000
        ) as resposta:
            page.locator("#btn-submit").click()
        status = resposta.value.status
        page.wait_for_load_state("domcontentloaded", timeout=60000)
    except Exception:
        return "o Puzzle não respondeu à gravação em 60 s"
    if status >= 400:
        return f"o Puzzle respondeu {status} ao salvar"
    erro_na_tela = page.locator(".alert-danger:visible, .swal2-html-container:visible, .toast-error:visible")
    if erro_na_tela.count():
        return " ".join(erro_na_tela.first.inner_text().split())[:300]
    return None


def _open_itinerary_edit(page: Page, edit_url: str) -> None:
    """Abre a tela de edição e espera a lista de lojas (sem "networkidle": trava com o Puzzle lento)."""
    page.goto(edit_url, wait_until="domcontentloaded", timeout=60000)
    page.locator("li.dd-item").first.wait_for(state="attached", timeout=60000)


def activate_brands_in_itinerary(
    context: BrowserContext, itinerary_id: int, itens: list, canonica=normalize_text, quando: str = "",
) -> dict:
    """Liga, num itinerário já identificado, as marcas de cada loja e salva UMA vez.

    Se a conferência não mostrar a marca ativa, reabre a rota, liga de novo e salva — até
    TENTATIVAS_SALVAR vezes no total; depois marca erro e segue para o próximo.
    Usado pelo reagendamento (rota de hoje) e pelo atendimento extra (rota de um dia escolhido).
    :param itens: [{"id", "industria", "ponto_de_venda"}] cujas lojas estão nesse itinerário
    :param quando: texto para as mensagens (ex.: "de hoje", "de 05/10/2026")
    :return: {id: {"status": "success" | "already_active" | "error", "message", "itinerary_id"}}
    """
    rota = f"na rota {quando}".strip() if quando else "na rota"
    edit_url = f"{BASE_URL}/itineraries/edit/{itinerary_id}"
    resultados = {}
    page = context.new_page()

    def resultado(status, message):
        return {"status": status, "message": message, "itinerary_id": itinerary_id}

    try:
        _open_itinerary_edit(page, edit_url)

        # 1. Separa o que já está ativo / sem contrato do que precisa ser ligado
        pendentes = []   # (item, contract_id)
        for item in itens:
            contract, motivo = _find_contract(page, item["ponto_de_venda"], item["industria"], canonica)
            if contract is None:
                resultados[item["id"]] = resultado("error", f"{motivo} (itinerário {itinerary_id})")
            elif _is_contract_active(contract):
                resultados[item["id"]] = resultado(
                    "already_active",
                    f"Marca '{item['industria']}' já estava ativa {rota} (itinerário {itinerary_id}). Nada alterado.",
                )
            else:
                pendentes.append((item, contract.get_attribute("id").replace("contract_", "")))

        # 2. Liga, salva e confere; se não confirmar, tenta de novo (até TENTATIVAS_SALVAR vezes)
        aviso = None
        for tentativa in range(1, TENTATIVAS_SALVAR + 1):
            if not pendentes:
                break
            if tentativa > 1:
                # A tela já foi recarregada na conferência anterior: liga de novo e salva
                print(f"      tentativa {tentativa}/{TENTATIVAS_SALVAR} no itinerário {itinerary_id}...")

            for item, contract_id in pendentes:
                contract = page.locator(f"small#contract_{contract_id}")
                enable_btn = contract.locator("a.reEnable")
                if "d-none" not in (enable_btn.get_attribute("class") or ""):
                    enable_btn.click()
                    expect(enable_btn).to_have_class(re.compile(r"\bd-none\b"), timeout=5000)
                logger.info(f"Contrato {contract_id} ({item['industria']}) ligado no itinerário {itinerary_id}"
                            f" (tentativa {tentativa}).")

            aviso = _salvar_rota(page) or aviso

            # Confere recarregando a edição: contratos ativos vêm com input hidden contract_id[ID]
            _open_itinerary_edit(page, edit_url)
            ainda_pendentes = []
            for item, contract_id in pendentes:
                if page.locator(f'input[name="contract_id[{contract_id}]"]').count() > 0:
                    detalhe = f" na {tentativa}ª tentativa" if tentativa > 1 else ""
                    resultados[item["id"]] = resultado(
                        "success", f"Marca '{item['industria']}' ativada {rota} (itinerário {itinerary_id}){detalhe}."
                    )
                else:
                    ainda_pendentes.append((item, contract_id))
            pendentes = ainda_pendentes

        for item, _ in pendentes:
            motivo = f" Aviso do Puzzle: {aviso}" if aviso else ""
            resultados[item["id"]] = resultado(
                "error",
                f"O salvamento não foi confirmado após {TENTATIVAS_SALVAR} tentativas: a marca '{item['industria']}'"
                f" não aparece ativa no itinerário {itinerary_id}.{motivo}",
            )
        return resultados

    except Exception as e:
        logger.error(f"Erro no Playwright ao processar o itinerário {itinerary_id}: {str(e)}")
        for item in itens:
            resultados.setdefault(item["id"], {
                "status": "error",
                "message": f"Falha na automação DOM: {str(e)}",
                "itinerary_id": itinerary_id,
            })
        return resultados
    finally:
        page.close()
