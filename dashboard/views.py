# dashboard/views.py

import concurrent.futures
import json
import os
import threading
from datetime import datetime, timedelta
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.text import slugify
from django.views.decorators.http import require_POST
from playwright.sync_api import sync_playwright

from rules_engine.models import ClientScheduleConfig, Justificativa, RescheduleProposal
from rules_engine.services.auditor import audit_contracts_vs_routes
from rules_engine.services.brands import (
    load_activity_industries,
    load_brand_equivalents,
    make_canonical,
    save_activity_industries,
)
from rules_engine.services.contract_history import compute_contract_starts, save_contract_presence
from rules_engine.services.frequency_checker import SITUACAO_CONTRATO_NOVO, analyze_reschedules
from scraping.services import calendar_automation as ca
from scraping.services.calendar_automation import (
    apply_reschedules_for_promoter,
    mark_already_scheduled_today,
)
from scraping.services.exporter import run_full_scraping_and_conversion
from scraping.services.puzzle_session import puzzle_login

# Propostas que podem ser (re)executadas pelo operador
STATUS_EXECUTAVEIS = ["PENDENTE", "ERRO"]

# Justificativa do operador (ex.: feriado), válida até o fim da semana
SITUACAO_JUSTIFICADO = "justificado"

# Situação da rota de hoje (apurada na sincronização) -> status da proposta
STATUS_POR_SITUACAO = {
    ca.SITUACAO_AGENDADA: "AGENDADO_HOJE",
    ca.SITUACAO_COBERTA: "COBERTA",
    ca.SITUACAO_FINALIZADA: "ROTA_FINALIZADA",
    ca.SITUACAO_SEM_ROTA: "SEM_ROTA_HOJE",
    ca.SITUACAO_FORA_DA_ROTA: "FORA_DA_ROTA",
    SITUACAO_CONTRATO_NOVO: "CONTRATO_NOVO",
    SITUACAO_JUSTIFICADO: "JUSTIFICADO",
}

# Status recalculados a cada sincronização (EXECUTADO/ERRO de hoje são preservados)
STATUS_RECALCULAVEIS = ["PENDENTE", *STATUS_POR_SITUACAO.values()]

# Uma automação por vez: evita duas sincronizações/execuções simultâneas (ex.: F5 durante o carregamento)
_automacao_em_andamento = threading.Lock()
MSG_EM_ANDAMENTO = (
    "Já existe uma sincronização ou execução em andamento. Aguarde ela terminar e atualize a página."
)


@login_required
def reschedule_dashboard(request):
    """Renderiza as propostas do dia com filtro por supervisor e ordenação hierárquica."""
    # O painel é operacional: mostra só as propostas geradas hoje (histórico fica no /admin)
    propostas_qs = RescheduleProposal.objects.filter(data_criacao__date=timezone.localdate())
    supervisores_list = (
        propostas_qs.values_list('supervisor', flat=True).distinct().order_by('supervisor')
    )

    # 1. Filtro dinâmico por Supervisor
    selected_supervisor = request.GET.get('supervisor', '').strip()
    if selected_supervisor:
        propostas_qs = propostas_qs.filter(supervisor__iexact=selected_supervisor)

    # 2. Ordenação Hierárquica: Supervisor -> Ponto de Venda (Loja) -> Promotor -> Marca
    propostas_qs = propostas_qs.order_by(
        'supervisor',
        'ponto_de_venda',
        'promotor_designado',
        'industria'
    )

    context = {
        "propostas": propostas_qs,
        "supervisores": [s for s in supervisores_list if s],
        "selected_supervisor": selected_supervisor,
        "status_executaveis": STATUS_EXECUTAVEIS,
        "total_pendentes": propostas_qs.filter(status="PENDENTE").count(),
        "total_agendados_hoje": propostas_qs.filter(status__in=["AGENDADO_HOJE", "COBERTA"]).count(),
        "total_sem_acao": propostas_qs.filter(
            status__in=["ROTA_FINALIZADA", "SEM_ROTA_HOJE", "FORA_DA_ROTA", "CONTRATO_NOVO", "JUSTIFICADO"]
        ).count(),
        "total_executados": propostas_qs.filter(status="EXECUTADO").count(),
        "total_erros": propostas_qs.filter(status="ERRO").count(),
    }
    return render(request, "dashboard/reschedule_list.html", context)


# Resumo por supervisor: status -> (categoria, rótulo para o supervisor, cor do selo)
RESUMO_STATUS = {
    "EXECUTADO": ("agendados_agora", "Agendado agora", "success"),
    "AGENDADO_HOJE": ("ja_agendados", "Já estava agendado", "info"),
    "COBERTA": ("ja_agendados", "Coberto por outro promotor", "coberta"),
    "PENDENTE": ("pendentes", "Pendente", "warning"),
    "APROVADO": ("pendentes", "Aprovado", "primary"),
    "ERRO": ("erros", "Erro", "danger"),
    "ROTA_FINALIZADA": ("sem_acao", "Rota finalizada", "dark"),
    "SEM_ROTA_HOJE": ("sem_acao", "Promotor sem rota hoje", "secondary"),
    "FORA_DA_ROTA": ("sem_acao", "Loja fora da rota", "secondary"),
    "CONTRATO_NOVO": ("sem_acao", "Contrato novo", "secondary"),
    "JUSTIFICADO": ("sem_acao", "Justificado", "secondary"),
    "REJEITADO": ("sem_acao", "Rejeitado", "secondary"),
}
# Status cujo motivo aparece também no texto e na imagem enviados ao supervisor
STATUS_COM_MOTIVO = ["ERRO", "JUSTIFICADO"]
# Rótulos curtos para a tabela do WhatsApp (cabe ~40 caracteres por linha no celular)
STATUS_CURTO = {
    "EXECUTADO": "Agendado",
    "AGENDADO_HOJE": "Já agendado",
    "COBERTA": "Coberto",
    "PENDENTE": "Pendente",
    "APROVADO": "Aprovado",
    "ERRO": "Erro",
    "ROTA_FINALIZADA": "Rota fechada",
    "SEM_ROTA_HOJE": "Sem rota",
    "FORA_DA_ROTA": "Fora da rota",
    "CONTRATO_NOVO": "Contrato novo",
    "JUSTIFICADO": "Justificado",
    "REJEITADO": "Rejeitado",
}
LARGURA_MARCA, LARGURA_STATUS = 16, 12


def tabela_whatsapp(lojas):
    """Tabela em fonte fixa (bloco ``` do WhatsApp): uma linha por marca, agrupada por loja.

    O nome completo da loja vira o título do grupo: com ~40 caracteres por linha no celular,
    uma coluna de loja obrigaria a abreviar demais (ex.: 'ASSAÍ SÃO PA 270').
    """
    linha = lambda marca, status: f"  {marca[:LARGURA_MARCA]:<{LARGURA_MARCA}} {status[:LARGURA_STATUS]}".rstrip()
    linhas = [linha("MARCA", "STATUS"), linha("-" * LARGURA_MARCA, "-" * LARGURA_STATUS)]
    for loja in lojas:
        linhas.append(loja["loja"])
        for item in loja["itens"]:
            linhas.append(linha(item["industria"], STATUS_CURTO.get(item["status"], item["rotulo"])))
    return "```\n" + "\n".join(linhas) + "\n```"


RESUMO_CATEGORIAS = [
    ("agendados_agora", "Agendados agora"),
    ("ja_agendados", "Já agendados/cobertos"),
    ("pendentes", "Pendentes"),
    ("erros", "Erros"),
    ("sem_acao", "Sem ação possível"),
]


def build_supervisor_summary(propostas, data):
    """Agrupa as propostas em supervisor -> loja -> marcas, com totais e texto para copiar.

    :return: [{"supervisor", "totais": [(rótulo, qtd)], "lojas": [{"loja", "cnpj", "itens"}], "texto"}]
    """
    por_supervisor = {}
    for p in propostas:
        categoria, rotulo, cor = RESUMO_STATUS.get(p.status, ("sem_acao", p.get_status_display(), "secondary"))
        sup = por_supervisor.setdefault(p.supervisor or "NÃO ATRIBUÍDO", {"lojas": {}, "totais": {}})
        sup["totais"][categoria] = sup["totais"].get(categoria, 0) + 1
        loja = sup["lojas"].setdefault(p.ponto_de_venda, {"loja": p.ponto_de_venda, "cnpj": p.cnpj, "itens": []})
        loja["itens"].append({
            "industria": p.industria,
            "promotor": p.promotor_designado,
            "rotulo": rotulo,
            "cor": cor,
            "status": p.status,
            "log": p.log_erro,
        })

    resumo = []
    for supervisor in sorted(por_supervisor):
        dados = por_supervisor[supervisor]
        lojas = sorted(dados["lojas"].values(), key=lambda l: l["loja"])
        for loja in lojas:
            loja["itens"].sort(key=lambda i: i["industria"])
        totais = [(rotulo, dados["totais"].get(cat, 0)) for cat, rotulo in RESUMO_CATEGORIAS]

        # Texto para WhatsApp: *negrito* no cabeçalho e tabela em bloco de fonte fixa
        linhas = [
            f"*Resumo do reagendamento - {data:%d/%m/%Y}*",
            f"*Supervisor:* {supervisor}",
            *(f"{rotulo}: {qtd}" for rotulo, qtd in totais),
            "",
            tabela_whatsapp(lojas),
        ]
        for status, titulo in (("ERRO", "*Erros:*"), ("JUSTIFICADO", "*Justificativas:*")):
            casos = [(loja, item) for loja in lojas for item in loja["itens"] if item["status"] == status]
            if casos:
                linhas += ["", titulo]
                linhas += [
                    f"- {loja['loja']} / {item['industria']}: {item['log'] or 'sem detalhe'}"
                    for loja, item in casos
                ]

        resumo.append({
            "supervisor": supervisor,
            "totais": totais,
            "lojas": lojas,
            "texto": "\n".join(linhas),
        })
    return resumo


@login_required
def supervisor_summary(request):
    """Resumo do reagendamento de hoje por supervisor: lojas, marcas e status de cada uma."""
    hoje = timezone.localdate()
    propostas_qs = RescheduleProposal.objects.filter(data_criacao__date=hoje)

    selected_supervisor = request.GET.get('supervisor', '').strip()
    if selected_supervisor:
        propostas_qs = propostas_qs.filter(supervisor__iexact=selected_supervisor)

    return render(request, "dashboard/supervisor_summary.html", {
        "resumo": build_supervisor_summary(propostas_qs, hoje),
        "hoje": hoje,
        "selected_supervisor": selected_supervisor,
    })


def render_png(html, seletor=".sheet"):
    """Gera um PNG (2x, nítido no celular) do elemento `seletor` de um HTML autocontido.

    Não acessa o banco: o sync_playwright bloqueia o ORM enquanto está aberto.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 960, "height": 800}, device_scale_factor=2)
            page.route("**/*", lambda route: route.abort())  # nada externo: só o HTML local
            page.set_content(html, wait_until="load")
            return page.locator(seletor).screenshot(type="png")
        finally:
            browser.close()


@login_required
def supervisor_summary_image(request):
    """Imagem (PNG) em formato de planilha com o resumo de hoje de UM supervisor, para o WhatsApp."""
    supervisor = request.GET.get('supervisor', '').strip()
    hoje = timezone.localdate()
    propostas = RescheduleProposal.objects.filter(data_criacao__date=hoje, supervisor__iexact=supervisor)
    resumo = build_supervisor_summary(propostas, hoje) if supervisor else []
    if not resumo:
        raise Http404("Nenhuma proposta de hoje para este supervisor.")

    sup = resumo[0]
    linha = 0
    for loja in sup["lojas"]:
        # Mesmo promotor em todas as marcas da loja: célula mesclada, como a da loja
        promotores = {item["promotor"] for item in loja["itens"]}
        loja["promotor_unico"] = promotores.pop() if len(promotores) == 1 else None
        for item in loja["itens"]:
            linha += 1
            item["linha"] = linha

    html = render_to_string("dashboard/summary_sheet.html", {
        "sup": sup, "hoje": hoje, "gerado_em": timezone.localtime(), "status_com_motivo": STATUS_COM_MOTIVO,
    })
    # Playwright em thread própria, como na execução (não conflita com o servidor)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        png = executor.submit(render_png, html).result()

    nome = slugify(sup["supervisor"]) or "supervisor"
    response = HttpResponse(png, content_type="image/png")
    disposicao = "attachment" if request.GET.get("download") else "inline"
    response["Content-Disposition"] = f'{disposicao}; filename="resumo_{nome}_{hoje:%Y-%m-%d}.png"'
    return response


def carregar_json(dado, nome_campo):
    """Garante o carregamento de dict/list, seja por memória, string JSON ou caminho de arquivo."""
    if isinstance(dado, (dict, list)):
        return dado

    if isinstance(dado, str):
        dado_limpo = dado.strip()

        if not dado_limpo:
            raise ValueError(f"O retorno de '{nome_campo}' veio vazio.")

        if os.path.exists(dado_limpo):
            with open(dado_limpo, "r", encoding="utf-8") as f:
                return json.load(f)

        if dado_limpo.startswith("{") or dado_limpo.startswith("["):
            return json.loads(dado_limpo)

        if "<html" in dado_limpo.lower() or "<doctype" in dado_limpo.lower():
            raise ValueError(
                f"O Puzzle retornou uma página HTML em '{nome_campo}' (verifique o login/sessão)."
            )

    raise ValueError(f"Formato não reconhecido para '{nome_campo}': {type(dado)}")


@login_required
@require_POST
def run_pipeline(request):
    """Executado quando o usuário clica no botão de sincronizar."""
    if not _automacao_em_andamento.acquire(blocking=False):
        messages.warning(request, MSG_EM_ANDAMENTO)
        return redirect("reschedule_dashboard")
    try:
        _sincronizar(request)
    finally:
        _automacao_em_andamento.release()
    return redirect("reschedule_dashboard")


def _sincronizar(request):
    try:
        # 1. Executa Scraping + Conversão + API de Rotas
        result = run_full_scraping_and_conversion()

        if result.get("planilha_reutilizada"):
            baixada_em = datetime.fromtimestamp(result["planilha_modificada_em"])
            messages.warning(
                request,
                "A exportação do Puzzle não retornou uma planilha nova; foi usada a última baixada"
                f" em {baixada_em:%d/%m/%Y %H:%M}. As visitas mais recentes podem não estar consideradas.",
            )

        # 2. Carrega com segurança os dados extraídos
        reagendamentos = carregar_json(result.get("reagendamentos_json"), "reagendamentos_json")
        rotas = carregar_json(result.get("rotas_json"), "rotas_json")

        # 3. Processa o Motor de Regras (contrato novo só conta a partir do dia em que apareceu)
        hoje = timezone.localdate()
        inicio_contratos, registro_contratos = compute_contract_starts(reagendamentos, hoje)
        propostas_geradas = analyze_reschedules(reagendamentos, rotas, inicio_contratos)
        novos_contratos = save_contract_presence(registro_contratos)

        # 3.1 Justificativas da semana (ex.: feriado) valem até domingo
        aplicar_justificativas(propostas_geradas, hoje)

        # 3.2 Consulta (somente leitura) a rota de hoje: já agendada, coberta, finalizada, fora da rota...
        marcas_conhecidas = {
            *(c.get("industria") for c in reagendamentos),
            *(r.get("industry_fullname") for r in rotas),
        }
        atividades_lidas = {}
        propostas_geradas = mark_already_scheduled_today(
            propostas_geradas, marcas_conhecidas, load_brand_equivalents(),
            atividades=load_activity_industries(), atividades_lidas=atividades_lidas,
        )
        save_activity_industries(atividades_lidas)

        # 4. Recalcula as propostas de hoje que ainda não foram tratadas.
        # EXECUTADO/ERRO de hoje são preservados; dias anteriores ficam como histórico.
        propostas_hoje = RescheduleProposal.objects.filter(data_criacao__date=hoje)
        propostas_hoje.filter(status__in=STATUS_RECALCULAVEIS).delete()

        novas = 0
        por_status = {}
        for item in propostas_geradas:
            chave = {
                "ponto_de_venda": item.get("cliente", ""),
                "industria": item.get("industria", ""),
                "cnpj": item.get("cnpj", "N/A"),
            }
            # Já tratada hoje (EXECUTADO/ERRO): mantém status e log, só atualiza os números
            existentes = propostas_hoje.filter(**chave)
            if existentes.exists():
                existentes.update(
                    dias_contratados=item.get("dias_contratados", "N/A"),
                    visitas_esperadas=item.get("visitas_esperadas", 1),
                    visitas_esperadas_ate_ontem=item.get("visitas_esperadas_ate_ontem", 0),
                    visitas_realizadas=item.get("visitas_realizadas", 0),
                )
                continue

            status = STATUS_POR_SITUACAO.get(item.get("situacao"), "PENDENTE")
            RescheduleProposal.objects.create(
                **chave,
                supervisor=item.get("supervisor", "NÃO ATRIBUÍDO"),
                dias_contratados=item.get("dias_contratados", "N/A"),
                visitas_esperadas=item.get("visitas_esperadas", 1),
                visitas_esperadas_ate_ontem=item.get("visitas_esperadas_ate_ontem", 0),
                visitas_realizadas=item.get("visitas_realizadas", 0),
                promotor_designado=item.get("promotor_designado", "Nenhum promotor"),
                promotor_id=item.get("promotor_id"),
                status=status,
                log_erro=item.get("log"),
            )
            novas += 1
            por_status[status] = por_status.get(status, 0) + 1

        rotulos = dict(RescheduleProposal.STATUS_CHOICES)
        detalhe = ", ".join(f"{qtd} {rotulos[s].lower()}" for s, qtd in sorted(por_status.items()))
        messages.success(
            request,
            f"Sincronização concluída com sucesso! {novas} propostas geradas"
            f"{f' ({detalhe})' if detalhe else ''}.",
        )
        if novos_contratos:
            messages.info(
                request,
                f"{novos_contratos} contrato(s) novo(s) apareceram hoje na planilha: as visitas de antes"
                " do primeiro dia não são cobradas.",
            )

    except Exception as e:
        messages.error(request, f"Erro ao sincronizar com o Puzzle: {str(e)}")


def _semana(dia):
    """Segunda-feira da semana do dia (a justificativa vale de segunda a domingo)."""
    return dia - timedelta(days=dia.weekday())


def aplicar_justificativas(propostas, hoje):
    """Marca como 'justificado' as propostas com justificativa do operador nesta semana."""
    justificativas = {
        (j.ponto_de_venda, j.industria, j.cnpj): j
        for j in Justificativa.objects.filter(semana=_semana(hoje)).select_related("usuario")
    }
    if not justificativas:
        return
    for item in propostas:
        j = justificativas.get((item.get("cliente", ""), item.get("industria", ""), item.get("cnpj", "N/A")))
        if j:
            item["situacao"] = SITUACAO_JUSTIFICADO
            item["log"] = _texto_justificativa(j)


def _texto_justificativa(j):
    quem = j.usuario.get_username() if j.usuario else "operador"
    return f"Justificado por {quem} em {timezone.localtime(j.criado_em):%d/%m %H:%M}: {j.motivo}"


@login_required
@require_POST
def justify_selected(request):
    """Justifica (ex.: feriado) as propostas selecionadas; vale para a loja + marca até o fim da semana."""
    proposta_ids = request.POST.getlist("proposta_ids")
    motivo = request.POST.get("motivo", "").strip()

    if not proposta_ids:
        messages.warning(request, "Selecione ao menos uma proposta para justificar.")
        return redirect("reschedule_dashboard")
    if not motivo:
        messages.warning(request, "Informe o motivo da justificativa.")
        return redirect("reschedule_dashboard")

    hoje = timezone.localdate()
    propostas = RescheduleProposal.objects.filter(
        id__in=proposta_ids, data_criacao__date=hoje, status__in=STATUS_EXECUTAVEIS
    )
    total = 0
    for prop in propostas:
        j, _ = Justificativa.objects.update_or_create(
            ponto_de_venda=prop.ponto_de_venda, industria=prop.industria, cnpj=prop.cnpj, semana=_semana(hoje),
            defaults={"motivo": motivo, "usuario": request.user},
        )
        prop.status = "JUSTIFICADO"
        prop.log_erro = _texto_justificativa(j)
        prop.save()
        total += 1

    if total:
        messages.success(request, f"{total} proposta(s) justificada(s) até o fim da semana: {motivo}")
    else:
        messages.warning(request, "Nenhuma das propostas selecionadas pode ser justificada (só pendentes ou com erro).")
    return redirect("reschedule_dashboard")


def _executar_no_puzzle(itens, canonica=None):
    """Aplica os reagendamentos no Puzzle SEM acessar o banco.

    O sync_playwright mantém um event loop ativo na thread enquanto está aberto, e o
    Django bloqueia o ORM nessa situação ("You cannot call this from an async context").
    Por isso recebe dados simples e devolve {id_proposta: resultado}.
    :param canonica: nome da marca -> marca com equivalências (ex.: LIBERTY = LIBERTY PLAST)
    """
    canonica = canonica or make_canonical()
    resultados = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=settings.PUZZLE_HEADLESS)
        context = browser.new_context()
        page = context.new_page()

        try:
            puzzle_login(page)

            # Agrupa por promotor: a rota de hoje é aberta e salva uma única vez por promotor
            por_promotor = {}
            for item in itens:
                por_promotor.setdefault(item["promotor_id"], []).append(item)

            total = len(por_promotor)
            print(f"--- [EXECUÇÃO] {len(itens)} proposta(s) de {total} promotor(es) ---")
            for n, (promoter_id, itens_promotor) in enumerate(por_promotor.items(), start=1):
                print(f"   [{n}/{total}] promotor {promoter_id}: {len(itens_promotor)} marca(s)")
                resultados.update(apply_reschedules_for_promoter(context, promoter_id, itens_promotor, canonica))
        except Exception as e:
            # Falha geral (ex.: login): as propostas ainda não processadas ficam em ERRO com o motivo
            for item in itens:
                resultados.setdefault(item["id"], {"status": "error", "message": f"Execução interrompida: {e}"})
        finally:
            browser.close()

    return resultados


def _processar_agendamentos_sincrono(proposta_ids):
    """Lê as propostas, executa no Puzzle e grava os resultados (ORM fora do Playwright)."""
    propostas = list(
        RescheduleProposal.objects.filter(id__in=proposta_ids, status__in=STATUS_EXECUTAVEIS)
    )

    if not propostas:
        return {}, "Nenhuma das propostas selecionadas está pendente ou existe."

    sem_promotor = [p for p in propostas if not p.promotor_id]
    executaveis = [p for p in propostas if p.promotor_id]

    resultados = {}
    if executaveis:
        # Equivalências lidas do banco ANTES de abrir o Playwright (ORM não roda dentro dele)
        canonica = make_canonical(load_brand_equivalents())
        resultados = _executar_no_puzzle([
            {
                "id": p.id,
                "promotor_id": p.promotor_id,
                "industria": p.industria,
                "ponto_de_venda": p.ponto_de_venda,
            }
            for p in executaveis
        ], canonica)

    # Contagem por supervisor: {supervisor: {"agendados_agora", "ja_agendados", "erros"}}
    resumo = {}

    def contar(prop, chave):
        sup = resumo.setdefault(prop.supervisor or "NÃO ATRIBUÍDO",
                                {"agendados_agora": 0, "ja_agendados": 0, "erros": 0})
        sup[chave] += 1

    for prop in sem_promotor:
        prop.status = "ERRO"
        prop.log_erro = "ID do promotor não encontrado nas rotas fixas."
        prop.save()
        contar(prop, "erros")

    for prop in executaveis:
        result = resultados.get(prop.id, {"status": "error", "message": "Sem retorno da automação."})
        status = result.get("status")
        if status == "success":
            prop.status = "EXECUTADO"
            prop.log_erro = result.get("message", "Sucesso")
            contar(prop, "agendados_agora")
        elif status == "already_active":
            prop.status = "AGENDADO_HOJE"
            prop.log_erro = result.get("message")
            contar(prop, "ja_agendados")
        else:
            prop.status = "ERRO"
            prop.log_erro = result.get("message", "Falha na aplicação")
            contar(prop, "erros")
        prop.save()

    return resumo, None


def _plural(qtd, singular, plural):
    return f"{qtd} {singular if qtd == 1 else plural}"


def mensagem_por_supervisor(supervisor, totais):
    """Ex.: 'Reagendamento Supervisor FULANO: 2 já estavam agendados, 1 foi agendado agora.'"""
    partes = [
        _plural(totais["ja_agendados"], "já estava agendado", "já estavam agendados"),
        _plural(totais["agendados_agora"], "foi agendado agora", "foram agendados agora"),
    ]
    if totais["erros"]:
        partes.append(_plural(totais["erros"], "com erro", "com erro"))
    return f"Reagendamento Supervisor {supervisor}: {', '.join(partes)}."


@login_required
@require_POST
def execute_selected_reschedules(request):
    """Executa a automação no calendário do Puzzle para as propostas selecionadas pelo operador."""
    proposta_ids = request.POST.getlist("proposta_ids")

    if not proposta_ids:
        messages.warning(request, "Nenhum reagendamento foi selecionado para execução.")
        return redirect('reschedule_dashboard')

    if not _automacao_em_andamento.acquire(blocking=False):
        messages.warning(request, MSG_EM_ANDAMENTO)
        return redirect('reschedule_dashboard')

    try:
        # O sync_playwright roda em thread própria para não conflitar com o loop do servidor
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_processar_agendamentos_sincrono, proposta_ids)
            resumo, aviso = future.result()

        if aviso:
            messages.warning(request, aviso)
        else:
            for supervisor in sorted(resumo):
                totais = resumo[supervisor]
                nivel = messages.warning if totais["erros"] else messages.success
                nivel(request, mensagem_por_supervisor(supervisor, totais))

    except Exception as e:
        messages.error(request, f"Erro na execução da automação Playwright: {str(e)}")
    finally:
        _automacao_em_andamento.release()

    return redirect('reschedule_dashboard')


@login_required
def audit_dashboard(request):
    """Exibe a lista de clientes, ordenando rigorosamente pendências e calculando o status real."""
    json_dir = Path(settings.MEDIA_ROOT) / "json_exports"
    contracts_json = json_dir / "Indicador_de_contratos.json"
    routes_json = json_dir / "fixed_routes.json"

    dados = audit_contracts_vs_routes(str(contracts_json), str(routes_json))

    if dados.get("erro_json"):
        messages.warning(
            request,
            "Aviso: Não foi possível ler o arquivo de contratos mais recente. Exibindo dados mantidos no banco de dados.",
        )

    # Carrega definições salvas no banco
    configs_banco = ClientScheduleConfig.objects.all()
    configs_salvas = {
        f"{cfg.rede.strip().upper()}_{cfg.industria.strip().upper()}_{cfg.frequencia_contratada.strip()}": cfg
        for cfg in configs_banco
    }

    clientes_finais = []

    # 1. Processa os itens mapeados
    if dados["clientes_para_configurar"]:
        for item in dados["clientes_para_configurar"]:
            key = f"{item['rede'].strip().upper()}_{item['industria'].strip().upper()}_{item['frequencia_contratada'].strip()}"
            cfg = configs_salvas.get(key)

            dias_salvos = []
            if cfg and cfg.dias_validados:
                dias_salvos = [
                    d.strip()
                    for d in cfg.dias_validados.split(",")
                    if d.strip()
                ]

            qtd_esperada = item["qtd_visitas"]

            # REGRA RIGOROSA DE PENDÊNCIA:
            # É pendente se NÃO tiver nenhum dia salvo OU se a quantidade de dias for diferente da frequência
            is_empty = len(dias_salvos) == 0
            is_mismatch = len(dias_salvos) != qtd_esperada
            tem_pendencia = is_empty or is_mismatch

            item["dias_salvos"] = dias_salvos
            item["tem_pendencia"] = tem_pendencia
            clientes_finais.append(item)
    else:
        # Fallback usando o banco de dados
        for cfg in configs_banco:
            dias_salvos = [
                d.strip() for d in cfg.dias_validados.split(",") if d.strip()
            ]

            try:
                qtd_esperada = int(cfg.frequencia_contratada.split("x")[0])
            except Exception:
                qtd_esperada = len(dias_salvos)

            tem_pendencia = (len(dias_salvos) == 0) or (
                len(dias_salvos) != qtd_esperada
            )

            clientes_finais.append({
                "rede": cfg.rede,
                "industria": cfg.industria,
                "frequencia_contratada": cfg.frequencia_contratada,
                "qtd_visitas": qtd_esperada,
                "exemplo_loja": "Cadastro do Banco",
                "dias_salvos": dias_salvos,
                "tem_pendencia": tem_pendencia,
            })

    # ORDENAÇÃO: Linhas com PENDÊNCIA (tem_pendencia=True) ficam OBRIGATORIAMENTE no TOPO
    clientes_finais.sort(
        key=lambda x: (
            not x["tem_pendencia"],  # False (Pendentes) vem primeiro
            x["rede"],
            x["industria"],
            x["qtd_visitas"],
        )
    )

    total_pendentes = sum(1 for c in clientes_finais if c["tem_pendencia"])
    dias_semana = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado"]

    context = {
        "clientes": clientes_finais,
        "total_clientes": len(clientes_finais),
        "total_pendentes": total_pendentes,
        "dias_semana": dias_semana,
    }
    return render(request, "dashboard/audit_schedules.html", context)


@login_required
@require_POST
def save_client_schedule(request):
    """Salva TODAS as alterações da tabela em lote (Massa)."""
    redes = request.POST.getlist("rede")
    industrias = request.POST.getlist("industria")
    frequencias = request.POST.getlist("frequencia_contratada")

    total_salvos = 0

    # Iteramos sobre cada linha enviada na tabela
    for i in range(len(redes)):
        rede = redes[i].strip().upper()
        industria = industrias[i].strip().upper()
        frequencia = frequencias[i].strip()

        # Chave do checkbox de cada linha (Ex: dias_semana_0, dias_semana_1, etc.)
        dias_selecionados = request.POST.getlist(f"dias_semana_{i}")
        dias_str = ", ".join(dias_selecionados)

        # Só salva/atualiza se houver dias selecionados ou se já existia registro
        if (
            dias_selecionados
            or ClientScheduleConfig.objects.filter(
                rede=rede,
                industria=industria,
                frequencia_contratada=frequencia,
            ).exists()
        ):
            ClientScheduleConfig.objects.update_or_create(
                rede=rede,
                industria=industria,
                frequencia_contratada=frequencia,
                defaults={"dias_validados": dias_str},
            )
            total_salvos += 1

    # Atualiza o JSON oficial de exportação
    export_schedules_to_json()

    messages.success(
        request,
        f"Sucesso! {total_salvos} regras de atendimento foram salvas/atualizadas em lote.",
    )

    return redirect("audit_dashboard")


def export_schedules_to_json():
    """Gera o ficheiro client_visit_schedules.json consolidado."""
    configs = ClientScheduleConfig.objects.all()
    lista_schedules = []

    dias_map = {
        "Segunda": 1,
        "Terça": 2,
        "Quarta": 3,
        "Quinta": 4,
        "Sexta": 5,
        "Sábado": 6,
        "Domingo": 7,
    }

    for cfg in configs:
        dias_list = [
            d.strip() for d in cfg.dias_validados.split(",") if d.strip()
        ]
        dias_num = [dias_map[d] for d in dias_list if d in dias_map]

        lista_schedules.append({
            "rede": cfg.rede,
            "industria": cfg.industria,
            "frequencia": cfg.frequencia_contratada,
            "dias_esperados": dias_list,
            "dias_semana_num": dias_num,
        })

    output_path = (
        Path(settings.MEDIA_ROOT)
        / "json_exports"
        / "client_visit_schedules.json"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(lista_schedules, f, ensure_ascii=False, indent=4)
