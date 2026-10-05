# dashboard/views_extra.py
"""Aba "Atendimento Extra": ligar uma marca num dia, nas lojas de uma rede ou escolhidas."""

import concurrent.futures
from datetime import date

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from dashboard.views import MSG_EM_ANDAMENTO, _automacao_em_andamento
from rules_engine.models import AtendimentoExtra, AtendimentoExtraItem
from rules_engine.services.brands import load_brand_equivalents, make_canonical
from scraping.services import extra_service as extra

# Lojas que ainda podem ser ligadas na confirmação (ERRO permite tentar de novo)
STATUS_A_EXECUTAR = ["PRONTO", "ERRO"]


def _indice():
    canonica = make_canonical(load_brand_equivalents())
    return extra.fixed_promoters_by_store(extra.load_fixed_routes(), canonica), canonica


def _em_thread(func, *args):
    """Playwright em thread própria (como no reagendamento): o banco fica fora dele."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(func, *args).result()


@login_required
def extra_form(request):
    """Formulário do atendimento extra + histórico dos pedidos."""
    indice, _ = _indice()
    historico = (
        AtendimentoExtra.objects.select_related("usuario")
        .annotate(
            total=Count("itens"),
            ligadas=Count("itens", filter=Q(itens__status="EXECUTADO")),
            erros=Count("itens", filter=Q(itens__status="ERRO")),
        )[:20]
    )
    return render(request, "dashboard/extra_form.html", {
        "marcas": extra.brands_with_fixed_routes(indice),
        "redes": extra.REDES,
        "hoje": timezone.localdate(),
        "historico": historico,
        "sem_rotas_fixas": not indice,
    })


@login_required
def extra_stores(request):
    """JSON com as lojas onde a marca tem promotor de rota fixa (para a seleção de lojas)."""
    indice, canonica = _indice()
    marca = request.GET.get("marca", "").strip()
    return JsonResponse({"lojas": extra.stores_for_brand(indice, marca, canonica) if marca else []})


@login_required
@require_POST
def extra_preview(request):
    """Gera a prévia (somente leitura no Puzzle) e grava o pedido."""
    marca = request.POST.get("marca", "").strip()
    escopo = request.POST.get("escopo", "rede")
    redes = [r for r in request.POST.getlist("redes") if r in extra.REDES]
    motivo = request.POST.get("motivo", "").strip()
    try:
        dia = date.fromisoformat(request.POST.get("data", ""))
    except ValueError:
        dia = None

    indice, canonica = _indice()
    erro = None
    if marca not in extra.brands_with_fixed_routes(indice):
        erro = "Escolha uma marca da lista."
    elif dia is None:
        erro = "Informe o dia do atendimento."
    elif dia < timezone.localdate():
        erro = "O dia precisa ser hoje ou uma data futura (rotas de dias passados ficam finalizadas)."

    if not erro:
        if escopo == "lojas":
            disponiveis = {l["loja"] for l in extra.stores_for_brand(indice, marca, canonica)}
            lojas = sorted({l for l in request.POST.getlist("lojas") if l in disponiveis})
            redes = []
        else:
            lojas = [l["loja"] for l in extra.stores_for_brand(indice, marca, canonica, redes)]
        if escopo != "lojas" and not redes:
            erro = "Escolha ao menos uma rede."
        elif not lojas:
            erro = "Nenhuma loja selecionada com promotor de rota fixa para essa marca."

    if erro:
        messages.warning(request, erro)
        return redirect("extra_form")

    if not _automacao_em_andamento.acquire(blocking=False):
        messages.warning(request, MSG_EM_ANDAMENTO)
        return redirect("extra_form")
    try:
        itens = _em_thread(extra.preview_extra, marca, dia, lojas, canonica, indice)
    except Exception as e:
        messages.error(request, f"Erro ao gerar a prévia no Puzzle: {e}")
        return redirect("extra_form")
    finally:
        _automacao_em_andamento.release()

    atendimento = AtendimentoExtra.objects.create(
        marca=marca, data=dia, redes=", ".join(redes), motivo=motivo, usuario=request.user,
    )
    AtendimentoExtraItem.objects.bulk_create([AtendimentoExtraItem(atendimento=atendimento, **i) for i in itens])
    messages.info(request, "Prévia gerada: nada foi alterado no Puzzle ainda. Confira e confirme abaixo.")
    return redirect("extra_detail", pk=atendimento.pk)


@login_required
def extra_detail(request, pk):
    """Prévia / resultado de um pedido de atendimento extra."""
    atendimento = get_object_or_404(AtendimentoExtra.objects.select_related("usuario"), pk=pk)
    itens = list(atendimento.itens.all())
    rotulos = dict(AtendimentoExtraItem.STATUS_CHOICES)
    contagem = {}
    for item in itens:
        contagem[item.status] = contagem.get(item.status, 0) + 1
    return render(request, "dashboard/extra_detail.html", {
        "atendimento": atendimento,
        "itens": itens,
        "contagem": [(rotulos[s], n, s) for s, n in sorted(contagem.items(), key=lambda x: -x[1])],
        "a_executar": sum(1 for i in itens if i.status in STATUS_A_EXECUTAR),
        "pode_executar": atendimento.data >= timezone.localdate(),
        "status_a_executar": STATUS_A_EXECUTAR,
    })


@login_required
@require_POST
def extra_execute(request, pk):
    """Liga a marca no Puzzle nas lojas prontas da prévia (e nas que deram erro, para tentar de novo)."""
    atendimento = get_object_or_404(AtendimentoExtra, pk=pk)
    if atendimento.data < timezone.localdate():
        messages.warning(request, "Esse dia já passou: as rotas ficam finalizadas e não podem ser alteradas.")
        return redirect("extra_detail", pk=pk)

    itens = list(atendimento.itens.filter(status__in=STATUS_A_EXECUTAR, promotor_id__isnull=False))
    if not itens:
        messages.warning(request, "Nenhuma loja pronta para ligar nesse pedido.")
        return redirect("extra_detail", pk=pk)

    if not _automacao_em_andamento.acquire(blocking=False):
        messages.warning(request, MSG_EM_ANDAMENTO)
        return redirect("extra_detail", pk=pk)
    try:
        canonica = make_canonical(load_brand_equivalents())
        dados = [{"id": i.id, "ponto_de_venda": i.ponto_de_venda, "promotor_id": i.promotor_id} for i in itens]
        resultados = _em_thread(extra.execute_extra, dados, atendimento.marca, atendimento.data, canonica)
    except Exception as e:
        messages.error(request, f"Erro na execução do atendimento extra: {e}")
        return redirect("extra_detail", pk=pk)
    finally:
        _automacao_em_andamento.release()

    ligadas = ja_ativas = erros = 0
    for item in itens:
        r = resultados.get(item.id, {"status": "error", "message": "Sem retorno da automação."})
        if r["status"] == "success":
            item.status, ligadas = "EXECUTADO", ligadas + 1
        elif r["status"] == "already_active":
            item.status, ja_ativas = "JA_ATIVO", ja_ativas + 1
        else:
            item.status, erros = "ERRO", erros + 1
        item.log = r.get("message", "")
        item.itinerary_id = r.get("itinerary_id") or item.itinerary_id
        item.save()

    atendimento.status = "PARCIAL" if atendimento.itens.filter(status="ERRO").exists() else "EXECUTADO"
    atendimento.executado_em = timezone.now()
    atendimento.save()

    nivel = messages.warning if erros else messages.success
    nivel(request, f"Atendimento extra de {atendimento.marca} em {atendimento.data:%d/%m/%Y}: {ligadas} loja(s)"
                   f" ligada(s) agora, {ja_ativas} já estava(m) ligada(s), {erros} com erro.")
    return redirect("extra_detail", pk=pk)
