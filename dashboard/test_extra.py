# dashboard/test_extra.py — Atendimento extra (ligar uma marca num dia)

from datetime import date, timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from rules_engine.models import AtendimentoExtra, AtendimentoExtraItem
from rules_engine.services.brands import make_canonical
from scraping.services import extra_service as extra
from scraping.tests import FakePuzzle, _edit_html, _evento, _loja

SABADO = date(2026, 10, 3)          # dia do atendimento extra (weekday 5)
TABOAO = "ASSAÍ TABOÃO DA SERRA (PIRAJUSSARA) - LJ 093"
SONIA = "ASSAÍ SÃO PAULO (VL. SÔNIA) - LJ 008"
BAURU = "ATACADÃO BAURU (CENTRO) - LJ 700"
CARREFOUR = "CARREFOUR SÃO PAULO (PINHEIROS) - LJ 1031"


def _rota_fixa(pid, nome, loja, marca, dia):
    return {"pearson_id": pid, "pearson_fullname": nome, "company_fullname": loja,
            "industry_fullname": marca, "day_of_week": dia, "week": 1}


ROTAS_FIXAS = [
    _rota_fixa(18337, "ELIANE", TABOAO, "QUELUZ", "Segunda"),
    _rota_fixa(12288, "RICLEI", TABOAO, "QUELUZ", "Sábado"),       # quem faz QUELUZ no Taboão aos sábados
    _rota_fixa(12288, "RICLEI", SONIA, "QUELUZ", "Sábado"),
    _rota_fixa(16248, "JULIA", BAURU, "QUELUZ", "Quarta"),
    _rota_fixa(16248, "JULIA", BAURU, "LIBERTY PLAST", "Quarta"),
    _rota_fixa(999, "OUTRA REDE", CARREFOUR, "QUELUZ", "Sábado"),  # fora do Assaí/Atacadão
]


class LojasDaMarcaTests(SimpleTestCase):
    def setUp(self):
        self.canonica = make_canonical({"LIBERTY": "LIBERTY PLAST"})
        self.indice = extra.fixed_promoters_by_store(ROTAS_FIXAS, self.canonica)

    def test_so_assai_e_atacadao(self):
        lojas = [l["loja"] for l in extra.stores_for_brand(self.indice, "QUELUZ", self.canonica)]
        self.assertEqual(lojas, [SONIA, TABOAO, BAURU])
        self.assertNotIn(CARREFOUR, lojas)

    def test_filtra_por_rede_e_lista_promotores(self):
        assai = extra.stores_for_brand(self.indice, "QUELUZ", self.canonica, ["ASSAÍ"])
        self.assertEqual([l["loja"] for l in assai], [SONIA, TABOAO])
        self.assertEqual(assai[1]["promotores"], ["ELIANE", "RICLEI"])

    def test_marca_equivalente(self):
        lojas = extra.stores_for_brand(self.indice, "LIBERTY", self.canonica)
        self.assertEqual([l["loja"] for l in lojas], [BAURU])

    def test_lista_de_marcas(self):
        self.assertEqual(extra.brands_with_fixed_routes(self.indice), ["LIBERTY PLAST", "QUELUZ"])


class PreviaTests(SimpleTestCase):
    """Atendimento extra de QUELUZ no sábado, 03/10/2026."""

    def _classificar(self, calendarios, telas, lojas, marca="QUELUZ"):
        canonica = make_canonical()
        indice = extra.fixed_promoters_by_store(ROTAS_FIXAS, canonica)
        self.client = FakePuzzle(calendarios, telas)
        itens = extra.classify_extra(self.client, marca, SABADO, lojas, canonica, indice)
        return {i["ponto_de_venda"]: i for i in itens}

    def test_liga_na_rota_do_promotor_de_rota_fixa_do_dia(self):
        itens = self._classificar(
            {
                12288: [_evento(900, "03/10/2026", [TABOAO, SONIA])],
                18337: [_evento(901, "03/10/2026", [TABOAO])],      # também passa no Taboão no sábado
            },
            {900: _edit_html(_loja(1, TABOAO, ["MUPY TP"]), _loja(2, SONIA, ["QUELUZ"])),
             901: _edit_html(_loja(1, TABOAO, ["QUELUZ"]))},
            [TABOAO, SONIA],
        )
        # Taboão: o promotor da rota fixa de sábado é o RICLEI (12288), não a ELIANE
        self.assertEqual((itens[TABOAO]["status"], itens[TABOAO]["promotor_id"], itens[TABOAO]["itinerary_id"]),
                         (extra.PRONTO, 12288, 900))
        self.assertIn("03/10/2026", itens[TABOAO]["log"])
        self.assertEqual(itens[SONIA]["status"], extra.JA_ATIVO)

    def test_sem_promotor_sem_rota_fora_da_rota_e_finalizada(self):
        itens = self._classificar(
            {
                12288: [_evento(900, "03/10/2026", [SONIA], editavel=False)],   # finalizada; Taboão fora
                16248: [_evento(902, "04/10/2026", [BAURU])],                   # sem rota no sábado
            },
            {},
            [TABOAO, SONIA, BAURU, "ASSAÍ SEM ROTA FIXA - LJ 1"],
        )
        self.assertEqual(itens[SONIA]["status"], extra.ROTA_FINALIZADA)
        self.assertEqual(itens[TABOAO]["status"], extra.FORA_DA_ROTA)
        self.assertEqual(itens[BAURU]["status"], extra.SEM_ROTA)
        self.assertEqual(itens["ASSAÍ SEM ROTA FIXA - LJ 1"]["status"], extra.SEM_PROMOTOR)

    def test_falha_na_consulta_fica_nao_verificado(self):
        canonica = make_canonical()
        indice = extra.fixed_promoters_by_store(ROTAS_FIXAS, canonica)
        quebrado = mock.Mock()
        quebrado.get.side_effect = RuntimeError("Puzzle fora do ar")
        itens = extra.classify_extra(quebrado, "QUELUZ", SABADO, [BAURU], canonica, indice)
        self.assertEqual(itens[0]["status"], extra.NAO_VERIFICADO)
        self.assertIn("Puzzle fora do ar", itens[0]["log"])


class TelasAtendimentoExtraTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("operador", password="x")
        self.client.force_login(self.user)
        self.amanha = timezone.localdate() + timedelta(days=1)
        patcher = mock.patch.object(extra, "load_fixed_routes", return_value=ROTAS_FIXAS)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _previa(self, **dados):
        base = {"marca": "QUELUZ", "data": self.amanha.isoformat(), "escopo": "rede", "redes": ["ASSAÍ"],
                "motivo": "Ação no fim de semana"}
        base.update(dados)
        itens = [{"ponto_de_venda": l, "promotor_designado": "RICLEI", "promotor_id": 12288, "itinerary_id": 900,
                  "status": extra.PRONTO, "log": "será ligada"} for l in [SONIA, TABOAO]]
        with mock.patch.object(extra, "preview_extra", return_value=itens) as previa:
            resp = self.client.post(reverse("extra_preview"), base, follow=True)
        return resp, previa

    def test_formulario_e_lojas_da_marca(self):
        resp = self.client.get(reverse("extra_form"))
        self.assertContains(resp, '<option value="QUELUZ">')
        lojas = self.client.get(reverse("extra_stores"), {"marca": "QUELUZ"}).json()["lojas"]
        self.assertEqual([l["loja"] for l in lojas], [SONIA, TABOAO, BAURU])

    def test_previa_da_rede_inteira_grava_o_pedido(self):
        resp, previa = self._previa()
        self.assertEqual(previa.call_args.args[2], [SONIA, TABOAO])   # só lojas do Assaí
        atendimento = AtendimentoExtra.objects.get()
        self.assertEqual((atendimento.marca, atendimento.data, atendimento.redes, atendimento.status),
                         ("QUELUZ", self.amanha, "ASSAÍ", "PREVIA"))
        self.assertEqual(atendimento.itens.count(), 2)
        self.assertContains(resp, "nada foi alterado no Puzzle ainda")
        self.assertContains(resp, "Confirmar e ligar em 2 loja(s)")

    def test_previa_de_lojas_escolhidas_ignora_loja_sem_rota_fixa(self):
        _, previa = self._previa(escopo="lojas", lojas=[BAURU, "LOJA INVENTADA"])
        self.assertEqual(previa.call_args.args[2], [BAURU])

    def test_validacoes(self):
        ontem = (timezone.localdate() - timedelta(days=1)).isoformat()
        resp, previa = self._previa(data=ontem)
        self.assertContains(resp, "O dia precisa ser hoje ou uma data futura")
        resp, _ = self._previa(marca="MARCA QUE NAO EXISTE")
        self.assertContains(resp, "Escolha uma marca da lista")
        resp, _ = self._previa(redes=[])
        self.assertContains(resp, "Escolha ao menos uma rede")
        previa.assert_not_called()
        self.assertFalse(AtendimentoExtra.objects.exists())

    def test_confirmar_liga_a_marca_e_grava_o_resultado(self):
        self._previa()
        atendimento = AtendimentoExtra.objects.get()
        sonia = atendimento.itens.get(ponto_de_venda=SONIA)
        taboao = atendimento.itens.get(ponto_de_venda=TABOAO)
        resultados = {
            sonia.id: {"status": "success", "message": "Marca 'QUELUZ' ativada na rota de ...", "itinerary_id": 900},
            taboao.id: {"status": "error", "message": "A loja saiu da rota", "itinerary_id": 900},
        }
        with mock.patch.object(extra, "execute_extra", return_value=resultados) as executar:
            resp = self.client.post(reverse("extra_execute", args=[atendimento.pk]), follow=True)
        dados = executar.call_args.args[0]
        self.assertEqual(sorted(d["ponto_de_venda"] for d in dados), [SONIA, TABOAO])
        self.assertEqual(executar.call_args.args[1:3], ("QUELUZ", self.amanha))
        sonia.refresh_from_db(); taboao.refresh_from_db(); atendimento.refresh_from_db()
        self.assertEqual((sonia.status, taboao.status, atendimento.status), ("EXECUTADO", "ERRO", "PARCIAL"))
        self.assertContains(resp, "1 loja(s) ligada(s) agora, 0 já estava(m) ligada(s), 1 com erro")
        # A loja com erro pode ser tentada de novo
        self.assertContains(resp, "Confirmar e ligar em 1 loja(s)")

    def test_nao_executa_dia_que_ja_passou(self):
        self._previa()
        atendimento = AtendimentoExtra.objects.get()
        AtendimentoExtra.objects.filter(pk=atendimento.pk).update(data=timezone.localdate() - timedelta(days=1))
        with mock.patch.object(extra, "execute_extra") as executar:
            resp = self.client.post(reverse("extra_execute", args=[atendimento.pk]), follow=True)
        executar.assert_not_called()
        self.assertContains(resp, "Esse dia já passou")

    def test_respeita_a_trava_de_automacao(self):
        from dashboard import views

        views._automacao_em_andamento.acquire()
        try:
            resp, previa = self._previa()
        finally:
            views._automacao_em_andamento.release()
        previa.assert_not_called()
        self.assertContains(resp, "Já existe uma sincronização ou execução em andamento")

    def test_link_no_painel(self):
        self.assertContains(self.client.get(reverse("reschedule_dashboard")), reverse("extra_form"))
