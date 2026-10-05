# dashboard/tests.py

from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from rules_engine.models import RescheduleProposal


def _proposta(**extra):
    base = {
        "cliente": "ATACADÃO INDAIATUBA (DISTRITO INDUSTRIAL) - LJ 658",
        "industria": "MILHO DE OURO",
        "cnpj": "75315333000109",
        "supervisor": "LARISSA",
        "dias_contratados": "Segunda, Quarta, Sexta",
        "visitas_esperadas": 3,
        "visitas_realizadas": 0,
        "promotor_designado": "JULIA",
        "promotor_id": 16248,
        "situacao": "a_reagendar",
    }
    base.update(extra)
    return base


class AcessoTests(TestCase):
    def test_telas_exigem_login(self):
        for nome in ["reschedule_dashboard", "audit_dashboard", "supervisor_summary"]:
            resp = self.client.get(reverse(nome))
            self.assertRedirects(resp, f"{reverse('login')}?next={reverse(nome)}")

    def test_acoes_so_aceitam_post(self):
        user = get_user_model().objects.create_user("operador", password="x")
        self.client.force_login(user)
        for nome in ["run_pipeline", "execute_selected", "save_client_schedule"]:
            self.assertEqual(self.client.get(reverse(nome)).status_code, 405)

    def test_telas_abrem_logado(self):
        user = get_user_model().objects.create_user("operador", password="x")
        self.client.force_login(user)
        self.assertEqual(self.client.get(reverse("reschedule_dashboard")).status_code, 200)
        self.assertEqual(self.client.get(reverse("audit_dashboard")).status_code, 200)


@override_settings(PUZZLE_HEADLESS=True)
@mock.patch("dashboard.views.puzzle_login")
class ExecucaoTests(TransactionTestCase):
    """Roda com o sync_playwright real (Chromium local); login e Puzzle são simulados.

    TransactionTestCase: a execução roda em outra thread (ThreadPoolExecutor), que não
    enxerga dados criados dentro da transação de um TestCase comum.
    """

    def setUp(self):
        user = get_user_model().objects.create_user("operador", password="x")
        self.client.force_login(user)
        base = dict(cnpj="1", supervisor="S", dias_contratados="Segunda", promotor_designado="P")
        self.ok = RescheduleProposal.objects.create(
            ponto_de_venda="LOJA A", industria="MARCA A", promotor_id=16248, **base)
        self.falha = RescheduleProposal.objects.create(
            ponto_de_venda="LOJA B", industria="MARCA B", promotor_id=16248, **base)
        self.sem_promotor = RescheduleProposal.objects.create(
            ponto_de_venda="LOJA C", industria="MARCA C", promotor_id=None, **base)

    def _executar(self, resultado_por_marca):
        """resultado_por_marca(industria) -> {"status", "message"}; simula o Puzzle por promotor."""
        self.chamadas = []

        def apply(context, promoter_id, itens, *args):
            self.chamadas.append((promoter_id, sorted(i["industria"] for i in itens)))
            return {i["id"]: resultado_por_marca(i["industria"]) for i in itens}

        with mock.patch("dashboard.views.apply_reschedules_for_promoter", side_effect=apply):
            return self.client.post(
                reverse("execute_selected"),
                {"proposta_ids": [self.ok.id, self.falha.id, self.sem_promotor.id]},
                follow=True,
            )

    def test_execucao_grava_resultados_sem_erro_de_contexto_async(self, _):
        def apply(marca):
            if marca == "MARCA A":
                return {"status": "success", "message": "Marca ativada (itinerário 1)."}
            return {"status": "error", "message": "Loja fora da rota de hoje."}

        resp = self._executar(apply)
        self.assertNotContains(resp, "async context")
        # As duas propostas do promotor 16248 vão juntas: a rota é aberta e salva uma vez só
        self.assertEqual(self.chamadas, [(16248, ["MARCA A", "MARCA B"])])
        self.assertContains(
            resp,
            "Reagendamento Supervisor S: 0 já estavam agendados, 1 foi agendado agora, 2 com erro.",
        )

        for prop in (self.ok, self.falha, self.sem_promotor):
            prop.refresh_from_db()
        self.assertEqual((self.ok.status, self.ok.log_erro), ("EXECUTADO", "Marca ativada (itinerário 1)."))
        self.assertEqual((self.falha.status, self.falha.log_erro), ("ERRO", "Loja fora da rota de hoje."))
        self.assertEqual(self.sem_promotor.status, "ERRO")

    def test_marca_ja_ativa_vira_ja_agendada_e_mensagem_por_supervisor(self, _):
        RescheduleProposal.objects.filter(id=self.falha.id).update(supervisor="OUTRO")
        RescheduleProposal.objects.filter(id=self.sem_promotor.id).delete()

        resp = self._executar(lambda marca: {"status": "already_active", "message": "Já estava ativa. Nada alterado."})
        self.ok.refresh_from_db()
        self.assertEqual(self.ok.status, "AGENDADO_HOJE")
        self.assertContains(resp, "Reagendamento Supervisor OUTRO: 1 já estava agendado, 0 foram agendados agora.")
        self.assertContains(resp, "Reagendamento Supervisor S: 1 já estava agendado, 0 foram agendados agora.")

    def test_falha_geral_marca_erro_com_motivo(self, login):
        login.side_effect = RuntimeError("login recusado")
        self._executar(lambda marca: {"status": "success"})
        self.ok.refresh_from_db()
        self.assertEqual(self.ok.status, "ERRO")
        self.assertIn("login recusado", self.ok.log_erro)

    def test_nenhuma_pendente_mostra_aviso_e_nao_abre_o_puzzle(self, _):
        RescheduleProposal.objects.update(status="EXECUTADO")
        resp = self._executar(lambda marca: {"status": "success"})
        self.assertContains(resp, "Nenhuma das propostas selecionadas está pendente ou existe.")
        self.assertNotContains(resp, "Erro na execução")
        self.assertEqual(self.chamadas, [])

    def test_bloqueia_execucao_simultanea(self, _):
        from dashboard import views

        views._automacao_em_andamento.acquire()
        try:
            resp = self._executar(lambda marca: {"status": "success"})
        finally:
            views._automacao_em_andamento.release()
        self.assertContains(resp, "Já existe uma sincronização ou execução em andamento")
        self.assertEqual(self.chamadas, [])
        self.ok.refresh_from_db()
        self.assertEqual(self.ok.status, "PENDENTE")


class ResumoSupervisorTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_user("operador", password="x")
        self.client.force_login(user)
        criar = lambda sup, loja, marca, status, log=None: RescheduleProposal.objects.create(
            ponto_de_venda=loja, industria=marca, cnpj="1", supervisor=sup, dias_contratados="Segunda",
            promotor_designado="P", promotor_id=1, status=status, log_erro=log)
        criar("LARISSA", "ATACADÃO INDAIATUBA", "MILHO DE OURO", "EXECUTADO")
        criar("LARISSA", "ATACADÃO INDAIATUBA", "EMBAVI", "AGENDADO_HOJE")
        criar("LARISSA", "ASSAÍ TABOÃO", "QUELUZ", "COBERTA")
        criar("LARISSA", "ASSAÍ TABOÃO", "JURERE", "ERRO", "Loja fora da rota de hoje.")
        criar("CAROLINE", "ASSAÍ JOÃO DIAS", "USIBRÁS", "ROTA_FINALIZADA")
        antiga = criar("CAROLINE", "ASSAÍ JOÃO DIAS", "BEGIN", "PENDENTE")
        RescheduleProposal.objects.filter(id=antiga.id).update(data_criacao=timezone.now() - timedelta(days=1))

    def test_agrupa_por_supervisor_e_loja_com_totais(self):
        resumo = self.client.get(reverse("supervisor_summary")).context["resumo"]
        self.assertEqual([s["supervisor"] for s in resumo], ["CAROLINE", "LARISSA"])

        larissa = resumo[1]
        self.assertEqual([l["loja"] for l in larissa["lojas"]], ["ASSAÍ TABOÃO", "ATACADÃO INDAIATUBA"])
        self.assertEqual(dict(larissa["totais"]), {
            "Agendados agora": 1, "Já agendados/cobertos": 2, "Pendentes": 0, "Erros": 1, "Sem ação possível": 0,
        })
        # Só propostas de hoje: a PENDENTE de ontem da CAROLINE não entra
        self.assertEqual(dict(resumo[0]["totais"])["Pendentes"], 0)
        self.assertEqual(dict(resumo[0]["totais"])["Sem ação possível"], 1)

    def test_texto_para_copiar(self):
        larissa = self.client.get(reverse("supervisor_summary")).context["resumo"][1]
        texto = larissa["texto"]
        self.assertIn(f"*Resumo do reagendamento - {timezone.localdate():%d/%m/%Y}*", texto)
        self.assertIn("*Supervisor:* LARISSA", texto)
        self.assertIn("Agendados agora: 1\nJá agendados/cobertos: 2\nPendentes: 0\nErros: 1", texto)

        # Tabela em bloco de fonte fixa do WhatsApp: loja como título, uma linha por marca
        tabela = texto.split("```")[1]
        self.assertEqual(tabela.strip("\n").splitlines(), [
            "  MARCA            STATUS",
            "  ---------------- ------------",
            "ASSAÍ TABOÃO",
            "  JURERE           Erro",
            "  QUELUZ           Coberto",
            "ATACADÃO INDAIATUBA",
            "  EMBAVI           Já agendado",
            "  MILHO DE OURO    Agendado",
        ])
        self.assertTrue(all(len(l) <= 40 for l in tabela.splitlines() if l.startswith("  ")))
        self.assertIn("*Erros:*\n- ASSAÍ TABOÃO / JURERE: Loja fora da rota de hoje.", texto)

    def test_imagem_png_da_planilha_do_supervisor(self):
        resp = self.client.get(reverse("supervisor_summary_image"), {"supervisor": "larissa"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "image/png")
        self.assertTrue(resp.content.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertIn('inline; filename="resumo_larissa_', resp["Content-Disposition"])

        baixar = self.client.get(reverse("supervisor_summary_image"), {"supervisor": "LARISSA", "download": "1"})
        self.assertIn("attachment;", baixar["Content-Disposition"])

    def test_imagem_de_supervisor_sem_propostas_hoje_da_404(self):
        self.assertEqual(self.client.get(reverse("supervisor_summary_image"), {"supervisor": "NINGUEM"}).status_code, 404)
        self.assertEqual(self.client.get(reverse("supervisor_summary_image")).status_code, 404)

    def test_planilha_mescla_promotor_quando_e_o_mesmo_na_loja(self):
        RescheduleProposal.objects.filter(industria="JURERE").update(promotor_designado="OUTRO")
        with mock.patch("dashboard.views.render_png", return_value=b"png") as render:
            self.client.get(reverse("supervisor_summary_image"), {"supervisor": "LARISSA"})
        html = render.call_args.args[0]
        # Indaiatuba: mesmo promotor nas 2 marcas -> 1 célula mesclada; Taboão: promotores diferentes -> por linha
        self.assertIn('<td class="promotor" rowspan="2">P</td>', html)
        self.assertIn("<td>OUTRO</td>", html)
        self.assertEqual(html.count('class="promotor"'), 1)

    def test_pagina_tem_botoes_de_imagem(self):
        resp = self.client.get(reverse("supervisor_summary"))
        self.assertContains(resp, "Copiar imagem")
        self.assertContains(resp, f'{reverse("supervisor_summary_image")}?supervisor=LARISSA&amp;download=1')

    def test_filtro_por_supervisor_e_botao_no_painel(self):
        resp = self.client.get(reverse("supervisor_summary"), {"supervisor": "caroline"})
        self.assertEqual([s["supervisor"] for s in resp.context["resumo"]], ["CAROLINE"])
        painel = self.client.get(reverse("reschedule_dashboard"), {"supervisor": "LARISSA"})
        self.assertContains(painel, f'{reverse("supervisor_summary")}?supervisor=LARISSA')


class AuditoriaTests(TestCase):
    @mock.patch("dashboard.views.export_schedules_to_json")
    def test_salvar_em_lote_associa_dias_a_linha_certa(self, _):
        from rules_engine.models import ClientScheduleConfig

        user = get_user_model().objects.create_user("operador", password="x")
        self.client.force_login(user)
        self.client.post(reverse("save_client_schedule"), {
            "rede": ["ASSAÍ", "ATACADÃO"],
            "industria": ["ITABOM", "URBANO"],
            "frequencia_contratada": ["2x por semana", "1x por semana"],
            "dias_semana_0": ["Terça", "Quinta"],
            "dias_semana_1": ["Sexta"],
        })
        regras = {c.industria: c.dias_validados for c in ClientScheduleConfig.objects.all()}
        self.assertEqual(regras, {"ITABOM": "Terça, Quinta", "URBANO": "Sexta"})


@mock.patch("dashboard.views.run_full_scraping_and_conversion",
            return_value={"reagendamentos_json": [], "rotas_json": []})
class SincronizacaoTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_user("operador", password="x")
        self.client.force_login(user)

    def _sincronizar(self, propostas):
        with mock.patch("dashboard.views.analyze_reschedules", return_value=propostas), \
             mock.patch("dashboard.views.mark_already_scheduled_today", side_effect=lambda p, *a, **k: p):
            self.client.post(reverse("run_pipeline"))

    def test_marca_ja_agendada_hoje(self, _):
        self._sincronizar([_proposta(situacao="agendada_hoje", log="já ativa")])
        prop = RescheduleProposal.objects.get()
        self.assertEqual(prop.status, "AGENDADO_HOJE")
        self.assertEqual(prop.log_erro, "já ativa")

    def test_situacao_da_rota_vira_status_nao_selecionavel(self, _):
        self._sincronizar([
            _proposta(industria="A", situacao="rota_finalizada", log="Rota finalizada (itinerário 773105)."),
            _proposta(industria="B", situacao="sem_rota_hoje"),
            _proposta(industria="C", situacao="fora_da_rota"),
            _proposta(industria="D", situacao="a_reagendar"),
            _proposta(industria="E", situacao="nao_verificada"),
        ])
        status = dict(RescheduleProposal.objects.values_list("industria", "status"))
        self.assertEqual(status, {
            "A": "ROTA_FINALIZADA", "B": "SEM_ROTA_HOJE", "C": "FORA_DA_ROTA",
            "D": "PENDENTE", "E": "PENDENTE",
        })
        resp = self.client.get(reverse("reschedule_dashboard"))
        self.assertContains(resp, "Rota finalizada (itinerário 773105).")
        self.assertEqual(resp.context["total_sem_acao"], 3)
        # Só as PENDENTES podem ser selecionadas
        habilitados = [p for p in resp.context["propostas"] if p.status in resp.context["status_executaveis"]]
        self.assertEqual(sorted(p.industria for p in habilitados), ["D", "E"])

    def test_contrato_novo_vira_status_proprio_e_avisa(self, _):
        self._sincronizar([_proposta(situacao="contrato_novo", log="Contrato novo: apareceu em 01/10/2026")])
        prop = RescheduleProposal.objects.get()
        self.assertEqual(prop.status, "CONTRATO_NOVO")
        resp = self.client.get(reverse("reschedule_dashboard"))
        self.assertContains(resp, "Contrato novo: apareceu em 01/10/2026")
        self.assertEqual(resp.context["total_sem_acao"], 1)

    def test_justificar_selecionadas_vale_ate_o_fim_da_semana(self, _):
        from rules_engine.models import Justificativa

        self._sincronizar([_proposta(industria="A"), _proposta(industria="B")])
        a = RescheduleProposal.objects.get(industria="A")
        resp = self.client.post(
            reverse("justify_selected"), {"proposta_ids": [a.id], "motivo": "Feriado municipal - Indaiatuba"},
            follow=True,
        )
        self.assertContains(resp, "1 proposta(s) justificada(s) até o fim da semana")
        a.refresh_from_db()
        self.assertEqual(a.status, "JUSTIFICADO")
        self.assertIn("Justificado por operador", a.log_erro)
        self.assertIn("Feriado municipal - Indaiatuba", a.log_erro)

        # Nova sincronização na mesma semana: continua justificada, sem consultar o Puzzle
        with mock.patch("dashboard.views.analyze_reschedules",
                        return_value=[_proposta(industria="A"), _proposta(industria="B")]), \
             mock.patch("dashboard.views.mark_already_scheduled_today", side_effect=lambda p, *a, **k: p) as mark:
            self.client.post(reverse("run_pipeline"))
        enviados = mark.call_args.args[0]
        self.assertEqual({p["industria"]: p.get("situacao") for p in enviados}, {"A": "justificado", "B": "a_reagendar"})
        self.assertEqual(RescheduleProposal.objects.get(industria="A").status, "JUSTIFICADO")

        # Na semana seguinte a justificativa não vale mais
        Justificativa.objects.update(semana=timezone.localdate() - timedelta(days=timezone.localdate().weekday() + 7))
        self._sincronizar([_proposta(industria="A")])
        self.assertEqual(RescheduleProposal.objects.get(industria="A").status, "PENDENTE")

    def test_justificar_exige_motivo_e_so_pendentes_ou_erro(self, _):
        self._sincronizar([_proposta(industria="A"), _proposta(industria="B", situacao="agendada_hoje")])
        a = RescheduleProposal.objects.get(industria="A")
        b = RescheduleProposal.objects.get(industria="B")
        sem_motivo = self.client.post(reverse("justify_selected"), {"proposta_ids": [a.id], "motivo": " "}, follow=True)
        self.assertContains(sem_motivo, "Informe o motivo")
        resp = self.client.post(reverse("justify_selected"), {"proposta_ids": [b.id], "motivo": "Feriado"}, follow=True)
        self.assertContains(resp, "Nenhuma das propostas selecionadas pode ser justificada")
        b.refresh_from_db()
        self.assertEqual(b.status, "AGENDADO_HOJE")

    def test_bloqueia_sincronizacao_simultanea(self, scraping):
        from dashboard import views

        views._automacao_em_andamento.acquire()
        try:
            resp = self.client.post(reverse("run_pipeline"), follow=True)
        finally:
            views._automacao_em_andamento.release()
        self.assertContains(resp, "Já existe uma sincronização ou execução em andamento")
        scraping.assert_not_called()

    def test_ressincronizar_recalcula_pendentes(self, _):
        self._sincronizar([_proposta()])
        self._sincronizar([_proposta(situacao="agendada_hoje")])
        self.assertEqual(RescheduleProposal.objects.count(), 1)
        self.assertEqual(RescheduleProposal.objects.get().status, "AGENDADO_HOJE")

    def test_erro_de_hoje_e_preservado_mas_numeros_atualizam(self, _):
        self._sincronizar([_proposta()])
        RescheduleProposal.objects.update(status="ERRO", log_erro="falhou")
        self._sincronizar([_proposta(visitas_esperadas_ate_ontem=1)])
        prop = RescheduleProposal.objects.get()
        self.assertEqual((prop.status, prop.log_erro), ("ERRO", "falhou"))
        self.assertEqual(prop.visitas_esperadas_ate_ontem, 1)

    def test_historico_de_outro_dia_nao_bloqueia_nova_proposta(self, _):
        self._sincronizar([_proposta()])
        RescheduleProposal.objects.update(
            status="EXECUTADO", data_criacao=timezone.now() - timedelta(days=7)
        )
        self._sincronizar([_proposta()])
        self.assertEqual(RescheduleProposal.objects.count(), 2)
        self.assertEqual(
            RescheduleProposal.objects.filter(data_criacao__date=timezone.localdate()).get().status,
            "PENDENTE",
        )

    def test_planilha_antiga_nao_apaga_propostas(self, _):
        self._sincronizar([_proposta()])
        with mock.patch("dashboard.views.analyze_reschedules",
                        side_effect=ValueError("A planilha do Puzzle é de 21/09 a 27/09 (semana 39)")):
            resp = self.client.post(reverse("run_pipeline"), follow=True)
        self.assertEqual(RescheduleProposal.objects.count(), 1)
        self.assertContains(resp, "semana 39")

    def test_erro_aparece_com_estilo_de_alerta(self, _):
        with mock.patch("dashboard.views.analyze_reschedules", side_effect=ValueError("falhou")):
            resp = self.client.post(reverse("run_pipeline"), follow=True)
        self.assertContains(resp, 'class="alert alert-danger')

    def test_painel_mostra_esperado_ate_ontem_e_motivo(self, _):
        self._sincronizar([_proposta(visitas_esperadas_ate_ontem=1, situacao="agendada_hoje",
                                     log="Marca já ativa (itinerário 772762).")])
        resp = self.client.get(reverse("reschedule_dashboard"))
        self.assertContains(resp, "0 de 1 até ontem")
        self.assertContains(resp, "Contrato: 3x/semana")
        self.assertContains(resp, "Marca já ativa (itinerário 772762).")

    def test_painel_mostra_apenas_propostas_de_hoje(self, _):
        self._sincronizar([_proposta()])
        RescheduleProposal.objects.update(data_criacao=timezone.now() - timedelta(days=7))
        resp = self.client.get(reverse("reschedule_dashboard"))
        self.assertEqual(len(resp.context["propostas"]), 0)
