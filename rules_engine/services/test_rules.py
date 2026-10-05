# rules_engine/services/test_rules.py

import os
import sys
import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.append(str(BASE_DIR))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')

import django
django.setup()

from django.conf import settings

from rules_engine.services.frequency_checker import analyze_reschedules


def test_rules():
    print("--- Testando Motor de Regras e Cruzamento de Dados ---")

    json_dir = Path(settings.MEDIA_ROOT) / "json_exports"
    reagendamentos_json = json_dir / "Indicador_de_contratos.json"
    fixed_routes_json = json_dir / "fixed_routes.json"

    if not reagendamentos_json.exists() or not fixed_routes_json.exists():
        print(f"Atenção: Garanta que ambos os arquivos JSON existem em {json_dir}")
        return

    # analyze_reschedules recebe as listas já carregadas, não os caminhos
    with open(reagendamentos_json, encoding="utf-8") as f:
        contratos = json.load(f)
    with open(fixed_routes_json, encoding="utf-8") as f:
        rotas = json.load(f)

    propostas = analyze_reschedules(contratos, rotas)

    print(f"\nTotal de clientes com proposta de reagendamento: {len(propostas)}")
    print(f"Com promotor identificado: {sum(1 for p in propostas if p['promotor_id'])}")
    if len(propostas) > 0:
        print("\nExemplo da primeira proposta gerada:")
        print(json.dumps(propostas[0], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    test_rules()
