# rules_engine/services/auditor.py

import json
from pathlib import Path

ALLOWED_REDES = ["ASSAÍ", "ASSAI", "ATACADÃO", "ATACADAO"]
EXCLUDED_INDUSTRIAS = ["PRATS"]


def extract_client_group(store_name: str) -> str:
    if not store_name:
        return "N/A"
    name_upper = store_name.strip().upper()
    if "ASSAÍ" in name_upper or "ASSAI" in name_upper:
        return "ASSAÍ"
    if "ATACADÃO" in name_upper or "ATACADAO" in name_upper:
        return "ATACADÃO"
    return name_upper.split()[0]


def extract_frequency_count(item: dict) -> int:
    freq_contratada = item.get("frequencia_contratada")
    if freq_contratada is not None:
        try:
            val = int(freq_contratada)
            if val > 0:
                return val
        except (ValueError, TypeError):
            pass

    dias_raw = item.get("dias") or item.get("dias_contratados") or ""
    if isinstance(dias_raw, str) and dias_raw.strip():
        dias_list = [
            d.strip()
            for d in dias_raw.split(",")
            if d.strip()
            and d.strip().lower()
            not in ["null", "none", "-", "não definido"]
        ]
        if len(dias_list) > 0:
            return len(dias_list)

    return 0


def audit_contracts_vs_routes(
    contracts_json_path: str, routes_json_path: str
) -> dict:
    """Extrai contratos ativas do Assaí/Atacadão para configuração."""
    if not Path(contracts_json_path).exists():
        # Retorna lista vazia; a view cuidará de preservar o banco de dados
        return {"clientes_para_configurar": [], "total_clientes": 0, "erro_json": True}

    try:
        with open(contracts_json_path, "r", encoding="utf-8") as f:
            contratos = json.load(f)
    except Exception:
        return {"clientes_para_configurar": [], "total_clientes": 0, "erro_json": True}

    clientes_map = {}

    for c in contratos:
        loja = (
            c.get("ponto_de_venda")
            or c.get("cliente")
            or c.get("company_fullname")
            or ""
        )
        rede = extract_client_group(loja)

        if rede not in ["ASSAÍ", "ATACADÃO"]:
            continue

        industria = (
            c.get("industria") or c.get("industry_fullname") or "N/A"
        ).upper()

        if industria in EXCLUDED_INDUSTRIAS:
            continue

        qtd_visitas = extract_frequency_count(c)
        if qtd_visitas == 0:
            continue

        freq_str = f"{qtd_visitas}x por semana"
        key = f"{rede}_{industria}_{freq_str}"

        if key not in clientes_map:
            clientes_map[key] = {
                "rede": rede,
                "industria": industria,
                "frequencia_contratada": freq_str,
                "qtd_visitas": qtd_visitas,
                "exemplo_loja": loja,
            }

    return {
        "clientes_para_configurar": list(clientes_map.values()),
        "total_clientes": len(clientes_map),
        "erro_json": False,
    }