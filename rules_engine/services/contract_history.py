# rules_engine/services/contract_history.py

from datetime import date, timedelta

from rules_engine.models import ContractPresence
from rules_engine.services.frequency_checker import contract_row_key


def compute_contract_starts(contracts_data: list, hoje: date) -> tuple:
    """Descobre quais contratos (loja + marca) são novos comparando com a sincronização anterior.

    Contrato novo = não estava na planilha da sincronização anterior (de um dia antes de hoje).
    Na primeira vez (histórico vazio), todos viram a base e nenhum é considerado novo.

    :return: (inicio_semana, registro)
        inicio_semana: {chave: primeiro_dia} só dos contratos que começaram nesta semana,
                       para o motor de regras (analyze_reschedules)
        registro: dados para save_contract_presence, chamado só depois do motor rodar sem erro
    """
    existentes = {p.chave: p for p in ContractPresence.objects.all()}
    historico_vazio = not existentes
    sincronizacao_anterior = max(
        (p.ultimo_dia for p in existentes.values() if p.ultimo_dia < hoje), default=None
    )

    linhas = {}
    for c in contracts_data:
        chave, loja, marca = contract_row_key(c)
        # Contrato = loja + marca: linha sem uma das duas não entra no histórico
        if str(loja).strip() and str(marca).strip():
            linhas.setdefault(chave, (loja, marca))

    inicio = {}
    for chave in linhas:
        atual = existentes.get(chave)
        if atual is None:
            inicio[chave] = None if historico_vazio else hoje
        elif atual.ultimo_dia >= hoje:
            inicio[chave] = atual.primeiro_dia          # já processado hoje
        elif sincronizacao_anterior and atual.ultimo_dia < sincronizacao_anterior:
            inicio[chave] = hoje                         # sumiu e voltou: contrato reiniciado
        else:
            inicio[chave] = atual.primeiro_dia

    segunda = hoje - timedelta(days=hoje.weekday())
    inicio_semana = {chave: dia for chave, dia in inicio.items() if dia and dia >= segunda}
    return inicio_semana, {"linhas": linhas, "inicio": inicio, "existentes": existentes, "hoje": hoje}


def save_contract_presence(registro: dict) -> int:
    """Grava a presença de hoje. :return: quantidade de contratos novos registrados hoje."""
    hoje, existentes = registro["hoje"], registro["existentes"]
    novos, atualizar = [], []
    for chave, (loja, marca) in registro["linhas"].items():
        primeiro_dia = registro["inicio"][chave]
        atual = existentes.get(chave)
        if atual is None:
            novos.append(ContractPresence(
                chave=chave, ponto_de_venda=loja, industria=marca,
                primeiro_dia=primeiro_dia, ultimo_dia=hoje,
            ))
        else:
            atual.primeiro_dia, atual.ultimo_dia = primeiro_dia, hoje
            atualizar.append(atual)

    ContractPresence.objects.bulk_create(novos, batch_size=500)
    ContractPresence.objects.bulk_update(atualizar, ["primeiro_dia", "ultimo_dia"], batch_size=500)
    return sum(1 for dia in registro["inicio"].values() if dia == hoje)
