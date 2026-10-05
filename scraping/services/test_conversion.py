# scraping/services/test_conversion.py

import json
import os
import sys
from pathlib import Path

# Configurar o ambiente do Django para permitir usar a settings
BASE_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.append(str(BASE_DIR))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")

import django

django.setup()

from scraping.utils.converters import convert_excel_to_json


def test_conversion():
    print("--- Testando conversão de .xlsx para .json ---")

    # Caminho do arquivo baixado na etapa anterior
    excel_path = BASE_DIR / "media" / "downloads" / "Indicador_de_contratos.xlsx"

    try:
        json_path = convert_excel_to_json(str(excel_path))

        # Verificar conteúdo gerado
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        print(f"Total de registros convertidos: {len(data)}")
        if len(data) > 0:
            print("\nExemplo do primeiro registro convertido:")
            print(json.dumps(data[0], ensure_ascii=False, indent=2))

    except Exception as e:
        print(f"Erro ao converter planilha: {e}")


if __name__ == "__main__":
    test_conversion()