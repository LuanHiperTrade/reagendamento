# rules_engine/services/brands.py

from datetime import timedelta

from django.utils import timezone

from rules_engine.models import AtividadeIndustria, MarcaEquivalente
from rules_engine.services.frequency_checker import normalize_text

# Atividades com indústria lida há mais tempo que isso são relidas do Puzzle
ATIVIDADE_VALIDADE = timedelta(days=7)


def load_activity_industries() -> dict:
    """Cache {id da atividade: indústria} das atividades do Puzzle ainda válidas.

    Atividades sem indústria cadastrada ou lidas há mais de 7 dias ficam de fora e são
    relidas na sincronização (assim correções feitas no Puzzle entram sozinhas).
    """
    limite = timezone.now() - ATIVIDADE_VALIDADE
    return dict(
        AtividadeIndustria.objects.filter(atualizado_em__gte=limite)
        .exclude(industria="")
        .values_list("survey_schedule_id", "industria")
    )


def save_activity_industries(lidas: dict) -> None:
    """Grava as atividades lidas do Puzzle nesta sincronização: {id: (nome, indústria)}."""
    for id_atividade, (nome, industria) in lidas.items():
        AtividadeIndustria.objects.update_or_create(
            survey_schedule_id=id_atividade, defaults={"nome": nome[:255], "industria": industria}
        )


def load_brand_equivalents() -> dict:
    """{nome alternativo: marca} cadastrados em /admin -> Marcas Equivalentes."""
    return dict(MarcaEquivalente.objects.values_list("nome", "marca"))


def make_canonical(equivalentes: dict | None = None):
    """Função nome -> marca normalizada, aplicando as equivalências (ex.: TUCS/FAZENDINHA -> BELA VISTA)."""
    mapa = {normalize_text(alt): normalize_text(marca) for alt, marca in (equivalentes or {}).items()}

    def canonica(nome: str) -> str:
        normalizado = normalize_text(nome)
        return mapa.get(normalizado, normalizado)

    return canonica
