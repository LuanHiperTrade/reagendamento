# scraping/utils/converters.py

import json
import re
from datetime import datetime
from pathlib import Path
import pandas as pd
from django.conf import settings


def extract_week_number_from_columns(columns) -> int | None:
    date_pattern = re.compile(r"^\d{2}/\d{2}/\d{4}$")
    for col in columns:
        if date_pattern.match(str(col).strip()):
            try:
                dt = datetime.strptime(str(col).strip(), "%d/%m/%Y")
                return dt.isocalendar()[1]
            except ValueError:
                continue
    return None


def convert_excel_to_json(file_path: str) -> str:
    """Lê a planilha de reagendamentos, normaliza colunas,

    trata campos nulos de promotores/dias e salva o JSON em
    media/json_exports/.
    """
    input_path = Path(file_path)

    if not input_path.exists():
        raise FileNotFoundError(
            f"Arquivo não encontrado para conversão: {file_path}"
        )

    json_dir = Path(settings.MEDIA_ROOT) / "json_exports"
    json_dir.mkdir(parents=True, exist_ok=True)

    output_json_path = json_dir / f"{input_path.stem}.json"

    if input_path.suffix.lower() in [".xlsx", ".xls"]:
        df = pd.read_excel(input_path)
    else:
        df = pd.read_csv(input_path)

    # Normalizar colunas
    new_columns = []
    date_columns = []
    date_pattern = re.compile(r"^\d{2}/\d{2}/\d{4}$")

    for col in df.columns:
        col_str = str(col).strip()
        if date_pattern.match(col_str):
            new_columns.append(col_str)
            date_columns.append(col_str)
        else:
            new_columns.append(col_str.lower().replace(" ", "_"))

    df.columns = new_columns

    # 1. Datas nulas -> 0
    for d_col in date_columns:
        df[d_col] = df[d_col].fillna(0).astype(int)

    # 2. Promotores nulos -> Texto padrão
    if "promotores" in df.columns:
        df["promotores"] = df["promotores"].fillna(
            "Nenhum promotor atendeu na semana"
        )

    # 3. Dias nulos -> Marcador para busca no JSON auxiliar / Interface Web
    if "dias" in df.columns:
        df["dias"] = df["dias"].fillna("NÃO DEFINIDO")

    # 4. Semana
    if "semana" in df.columns:
        calculated_week = extract_week_number_from_columns(date_columns)
        if calculated_week:
            df["semana"] = df["semana"].fillna(calculated_week).astype(int)

    if "cnpj" in df.columns:
        df["cnpj"] = df["cnpj"].fillna("0")

    if "supervisor" in df.columns:
        df["supervisor"] = df["supervisor"].fillna("0")

    # Tratar outros nulos restantes
    df = df.where(pd.notnull(df), None)

    records = df.to_dict(orient="records")

    with open(output_json_path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=4, default=str)

    print(
        f"Planilha convertida com SUCESSO e tratada para JSON:"
        f" {output_json_path}"
    )
    return str(output_json_path)