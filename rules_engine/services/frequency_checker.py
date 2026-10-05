# rules_engine/services/frequency_checker.py

from datetime import date, datetime, timedelta
import html
import json
import re
import unicodedata
from pathlib import Path
from django.conf import settings

ALLOWED_REDES = ["ASSAÍ", "ASSAI", "ATACADÃO", "ATACADAO"]
EXCLUDED_INDUSTRIAS = ["PRATS"]

# Situação definida já no motor: contrato surgiu nesta semana e o atraso é de antes dele existir
SITUACAO_CONTRATO_NOVO = "contrato_novo"

DAY_MAP = {
    "SEG": 0, "SEGUNDA": 0,
    "TER": 1, "TERÇA": 1, "TERCA": 1,
    "QUA": 2, "QUARTA": 2,
    "QUI": 3, "QUINTA": 3,
    "SEX": 4, "SEXTA": 4,
    "SAB": 5, "SÁBADO": 5, "SABADO": 5,
    "DOM": 6, "DOMINGO": 6,
}


def normalize_text(text: str) -> str:
    """Remove acentos, caracteres especiais e pontuações para cruzamento perfeito."""
    if not text:
        return ""
    # A API de rotas devolve entidades HTML (ex: "COCO &amp; CIA")
    text = html.unescape(str(text))
    text = unicodedata.normalize("NFD", text).encode("ascii", "ignore").decode("utf-8")
    text = re.sub(r"[^\w\s]", " ", text)
    return " ".join(text.upper().split())


def clean_cnpj(cnpj_raw: str) -> str:
    """Extrai apenas os dígitos numéricos do CNPJ."""
    if not cnpj_raw:
        return ""
    return re.sub(r"\D", "", str(cnpj_raw)).strip()


def get_day_index(dia_str: str) -> int:
    """Retorna o índice do dia da semana (0=Segunda, ..., 6=Domingo)."""
    dia_clean = normalize_text(dia_str)
    for key, idx in DAY_MAP.items():
        if key in dia_clean:
            return idx
    return -1


def is_today_in_expected_days(dias_esperados_list: list) -> bool:
    """Verifica se o dia atual faz parte da agenda de atendimento da loja."""
    today_idx = datetime.now().weekday()
    for dia in dias_esperados_list:
        if get_day_index(dia) == today_idx:
            return True
    return False


def calculate_expected_visits_until_yesterday(dias_esperados_list: list, desde: date | None = None) -> int:
    """Calcula quantas visitas eram esperadas estritamente em dias anteriores a hoje.

    :param desde: primeiro dia do contrato (contrato novo); dias anteriores não contam.
    """
    hoje = datetime.now().date()
    today_idx = hoje.weekday()
    segunda = hoje - timedelta(days=today_idx)
    esperadas_passadas = 0

    for dia in dias_esperados_list:
        idx = get_day_index(dia)
        if 0 <= idx < today_idx and (desde is None or segunda + timedelta(days=idx) >= desde):
            esperadas_passadas += 1

    return esperadas_passadas


def contract_row_key(c: dict) -> tuple:
    """(chave, loja, marca) de uma linha da planilha. A chave identifica o contrato loja + marca."""
    loja = c.get("ponto_de_venda") or c.get("cliente") or c.get("company_fullname") or ""
    marca = c.get("industria") or c.get("industry_fullname") or ""
    return f"{normalize_text(loja)}|{normalize_text(marca)}", loja, marca


def extract_client_group(store_name: str) -> str:
    """Extrai o grupo comercial (Assaí/Atacadão)."""
    if not store_name:
        return ""
    name_norm = normalize_text(store_name)
    if "ASSAI" in name_norm:
        return "ASSAÍ"
    if "ATACADAO" in name_norm:
        return "ATACADÃO"
    return ""


def calculate_realized_visits(item: dict) -> int:
    """
    Calcula as visitas realizadas analisando colunas de data (DD/MM/YYYY).
    Valores maiores que 0 no mesmo dia (ex: 2 atendimentos) contam como no máximo 1 visita.
    """
    realizadas = 0
    for k, v in item.items():
        if "/" in k and len(k) == 10:
            val_str = str(v).strip().replace(",", ".")
            try:
                val = float(val_str)
                if val > 0:
                    realizadas += 1
            except (ValueError, TypeError):
                if val_str.upper() in ["TRUE", "SIM", "1"]:
                    realizadas += 1
    return realizadas


def validate_report_is_current(contracts_data: list) -> None:
    """Bloqueia a análise se a planilha não cobrir o dia de hoje.

    As colunas DD/MM/YYYY da planilha indicam a semana do relatório. Se a exportação
    do Puzzle falhar, o exporter reaproveita a planilha anterior; comparar a semana
    antiga com o dia de hoje geraria propostas erradas.
    """
    # Formato: o relatório certo tem uma linha por loja + marca (coluna "industria") e 7 dias.
    # Ex. do erro em 02/10/2026: veio o relatório mensal agrupado por loja (49 linhas, sem marca, 35 dias).
    colunas = set().union(*(c.keys() for c in contracts_data[:50]))
    if not colunas & {"industria", "industry_fullname"}:
        raise ValueError(
            f"A planilha baixada do Puzzle não tem a coluna de indústria/marca ({len(contracts_data)} linhas):"
            " não é o Indicador de Contratos por loja + marca. Confira os filtros da exportação em"
            " /dashboards/indicators-contracts no Puzzle e sincronize de novo; nenhuma proposta foi alterada."
        )
    sem_marca = sum(1 for c in contracts_data if not (c.get("industria") or c.get("industry_fullname")))
    if sem_marca > len(contracts_data) / 2:
        raise ValueError(
            f"{sem_marca} de {len(contracts_data)} linhas da planilha do Puzzle vieram sem indústria/marca;"
            " confira os filtros da exportação e sincronize de novo. Nenhuma proposta foi alterada."
        )

    datas = set()
    for c in contracts_data[:50]:
        for k in c:
            try:
                datas.add(datetime.strptime(k, "%d/%m/%Y").date())
            except (TypeError, ValueError):
                continue

    if not datas:
        raise ValueError("A planilha do Puzzle não tem colunas de data; nenhuma proposta foi gerada.")

    if (max(datas) - min(datas)).days > 6:
        raise ValueError(
            f"A planilha do Puzzle cobre {(max(datas) - min(datas)).days + 1} dias ({min(datas):%d/%m} a"
            f" {max(datas):%d/%m}); o esperado é a semana atual (7 dias). Confira o período da exportação"
            " no Puzzle e sincronize de novo. Nenhuma proposta foi alterada."
        )

    hoje = datetime.now().date()
    if hoje not in datas:
        inicio, fim = min(datas), max(datas)
        raise ValueError(
            f"A planilha do Puzzle é de {inicio:%d/%m} a {fim:%d/%m} (semana {inicio.isocalendar()[1]}),"
            f" mas hoje é {hoje:%d/%m}. A exportação provavelmente falhou e a planilha antiga foi"
            f" reaproveitada; nenhuma proposta foi gerada."
        )


def choose_promoter(candidatos: list, dias_esperados_list: list) -> dict:
    """
    Escolhe o promotor entre as rotas da mesma [LOJA + INDÚSTRIA].
    Prioridade: quem atende HOJE (a execução reativa a marca na rota do dia),
    depois quem atende um dos dias esperados que já passaram, depois o primeiro encontrado.
    """
    if not candidatos:
        return {}

    today_idx = datetime.now().weekday()
    for info in candidatos:
        if info["dia_idx"] == today_idx:
            return info

    dias_passados = {
        get_day_index(d) for d in dias_esperados_list
        if 0 <= get_day_index(d) < today_idx
    }
    for info in candidatos:
        if info["dia_idx"] in dias_passados:
            return info

    return candidatos[0]


def analyze_reschedules(contracts_data: list, routes_data: list, inicio_contratos: dict | None = None) -> list:
    """
    Analisa os contratos vinculando promotores EXCLUSIVAMENTE pelo cruzamento de
    [LOJA + CLIENTE/INDÚSTRIA] no arquivo fixed_routes.json.

    :param inicio_contratos: {chave do contrato (contract_row_key): primeiro dia em que apareceu}
        para os contratos novos desta semana; as visitas de antes desse dia não são cobradas.
    """
    inicio_contratos = inicio_contratos or {}
    if not contracts_data:
        return []

    # 0. A planilha precisa ser da semana atual
    validate_report_is_current(contracts_data)

    # 1. Carrega Matriz de Dias Esperados
    schedules_json_path = (
        Path(settings.MEDIA_ROOT)
        / "json_exports"
        / "client_visit_schedules.json"
    )
    schedules_map = {}

    if schedules_json_path.exists():
        try:
            with open(schedules_json_path, "r", encoding="utf-8") as f:
                schedules_list = json.load(f)
                for s in schedules_list:
                    k = f"{normalize_text(s['rede'])}_{normalize_text(s['industria'])}_{str(s['frequencia']).strip()}"
                    schedules_map[k] = s.get("dias_esperados", [])
        except Exception:
            pass

    # 2. Indexa as Rotas Fixas por [CNPJ + INDÚSTRIA] e por [LOJA + INDÚSTRIA]
    promotores_by_cnpj = {}
    promotores_by_loja = {}

    if isinstance(routes_data, list):
        for r in routes_data:
            loja_rota = normalize_text(
                r.get("ponto_de_venda") or r.get("cliente") or r.get("company_fullname") or r.get("pdv") or r.get("loja")
            )
            ind_rota = normalize_text(
                r.get("industria") or r.get("industry_fullname") or r.get("cliente_marca") or r.get("marca") or r.get("cliente")
            )
            cnpj_rota = clean_cnpj(r.get("cnpj") or r.get("cnpj_cliente"))

            p_nome = (
                r.get("pearson_fullname")
                or r.get("promotor")
                or r.get("promotor_designado")
                or r.get("nome_promotor")
                or r.get("promoter_name")
            )
            p_id = (
                r.get("pearson_id")
                or r.get("promotor_id")
                or r.get("promoters_id")
                or r.get("id_promotor")
                or r.get("promoter_id")
            )

            if not p_nome or str(p_nome).strip().lower() in ["none", "null", ""]:
                continue

            info = {
                "promotor_designado": str(p_nome).strip(),
                "promotor_id": p_id,
                "dia_idx": get_day_index(r.get("day_of_week") or ""),
            }

            # Guarda todos os candidatos: a mesma loja/marca pode ter promotores diferentes por dia
            if cnpj_rota and ind_rota:
                promotores_by_cnpj.setdefault(f"{cnpj_rota}_{ind_rota}", []).append(info)

            if loja_rota and ind_rota:
                promotores_by_loja.setdefault(f"{loja_rota}_{ind_rota}", []).append(info)

    propostas = []

    for c in contracts_data:
        loja_raw = (
            c.get("ponto_de_venda")
            or c.get("cliente")
            or c.get("company_fullname")
            or ""
        )
        rede = extract_client_group(loja_raw)

        # REGRA 1: Apenas Assaí e Atacadão
        if not rede:
            continue

        industria_raw = c.get("industria") or c.get("industry_fullname") or ""
        industria_norm = normalize_text(industria_raw)

        # REGRA 2: Filtra marcas restritas
        if not industria_norm or industria_norm in EXCLUDED_INDUSTRIAS:
            continue

        # REGRA 3: Frequência contratada
        freq_raw = c.get("frequencia_contratada") or 0
        try:
            visitas_esperadas = int(freq_raw)
        except (ValueError, TypeError):
            visitas_esperadas = 0

        if visitas_esperadas <= 0:
            continue

        freq_str = f"{visitas_esperadas}x por semana"
        cfg_key = f"{normalize_text(rede)}_{industria_norm}_{freq_str}"
        dias_esperados_list = schedules_map.get(cfg_key, [])

        # REGRA 4A: Se o atendimento é previsto para o DIA ATUAL, descarta
        if is_today_in_expected_days(dias_esperados_list):
            continue

        # REGRA 4B: Avalia apenas visitas em atraso de DIAS ANTERIORES
        visitas_esperadas_ate_ontem = calculate_expected_visits_until_yesterday(dias_esperados_list)
        if visitas_esperadas_ate_ontem <= 0:
            continue

        # REGRA 5: Calcula visitas realizadas (máximo 1 por dia)
        visitas_realizadas = calculate_realized_visits(c)

        # REGRA 6: Contrato novo — só conta as visitas a partir do dia em que o contrato apareceu
        inicio = inicio_contratos.get(contract_row_key(c)[0])
        situacao = None
        if inicio and visitas_realizadas < visitas_esperadas_ate_ontem:
            esperadas_desde_inicio = calculate_expected_visits_until_yesterday(dias_esperados_list, desde=inicio)
            if visitas_realizadas >= esperadas_desde_inicio:
                # Só falta visita de antes do contrato existir: aparece no painel, sem reagendar
                situacao = SITUACAO_CONTRATO_NOVO
            else:
                visitas_esperadas_ate_ontem = esperadas_desde_inicio

        if visitas_realizadas < visitas_esperadas_ate_ontem:
            dias_str = (
                ", ".join(dias_esperados_list)
                if dias_esperados_list
                else "Não configurado"
            )

            # Cruzamento Obrigatório [LOJA + CLIENTE/INDÚSTRIA] no fixed_routes
            loja_norm = normalize_text(loja_raw)
            cnpj_contrato = clean_cnpj(c.get("cnpj"))

            key_cnpj_ind = f"{cnpj_contrato}_{industria_norm}" if cnpj_contrato else ""
            key_loja_ind = f"{loja_norm}_{industria_norm}"

            # 1ª tentativa: CNPJ + Indústria (imune a erros de digitação no nome da loja)
            # 2ª tentativa: Loja + Indústria
            candidatos = (
                promotores_by_cnpj.get(key_cnpj_ind)
                or promotores_by_loja.get(key_loja_ind)
                or []
            )
            info_rota = choose_promoter(candidatos, dias_esperados_list)

            # Promotor e ID vêm EXCLUSIVAMENTE do fixed_routes
            promotor_nome = info_rota.get("promotor_designado") or "Nenhum promotor nas rotas fixas"
            promotor_id = info_rota.get("promotor_id")

            proposta = {
                    "cliente": loja_raw,
                    "industria": industria_raw.strip().upper(),
                    "cnpj": c.get("cnpj", "N/A"),
                    "supervisor": (
                        c.get("supervisor") or "NÃO ATRIBUÍDO"
                    ).strip().upper(),
                    "dias_contratados": dias_str,
                    "visitas_esperadas": visitas_esperadas,
                    "visitas_esperadas_ate_ontem": visitas_esperadas_ate_ontem,
                    "visitas_realizadas": visitas_realizadas,
                    "promotor_designado": promotor_nome,
                    "promotor_id": promotor_id,
            }
            if situacao == SITUACAO_CONTRATO_NOVO:
                proposta["situacao"] = situacao
                proposta["log"] = (
                    f"Contrato novo: apareceu em {inicio:%d/%m/%Y}; as visitas anteriores não são cobradas."
                )
            elif inicio:
                proposta["log"] = f"Contrato novo desde {inicio:%d/%m/%Y}: visitas contadas a partir desse dia."
            propostas.append(proposta)

    return propostas