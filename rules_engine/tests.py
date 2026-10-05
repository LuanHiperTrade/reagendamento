# rules_engine/tests.py

import json
import tempfile
from datetime import date
from datetime import datetime as real_datetime
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase, TestCase, override_settings

from rules_engine.models import ContractPresence
from rules_engine.services import frequency_checker
from rules_engine.services.contract_history import compute_contract_starts, save_contract_presence
from rules_engine.services.frequency_checker import (
    SITUACAO_CONTRATO_NOVO,
    analyze_reschedules,
    contract_row_key,
    validate_report_is_current,
)


class FakeDatetime(real_datetime):
    """Fixa 'hoje' em terça, 29/09/2026."""
    @classmethod
    def now(cls, tz=None):
        return real_datetime(2026, 9, 29, 7, 0)


class QuintaFeira(real_datetime):
    """Fixa 'hoje' em quinta, 01/10/2026."""
    @classmethod
    def now(cls, tz=None):
        return real_datetime(2026, 10, 1, 7, 0)


def _contrato(datas, visitas=None):
    c = {"ponto_de_venda": "ASSAÍ BAURU (CENTRO) - LJ 061", "industria": "URBANO", "frequencia_contratada": 2}
    c.update({d: (visitas or {}).get(d, 0) for d in datas})
    return c


SEMANA_40 = ["28/09/2026", "29/09/2026", "30/09/2026", "01/10/2026", "02/10/2026", "03/10/2026", "04/10/2026"]
SEMANA_39 = ["21/09/2026", "22/09/2026", "23/09/2026", "24/09/2026", "25/09/2026", "26/09/2026", "27/09/2026"]


@mock.patch.object(frequency_checker, "datetime", FakeDatetime)
class PlanilhaDaSemanaTests(SimpleTestCase):
    def test_planilha_da_semana_atual_passa(self):
        validate_report_is_current([_contrato(SEMANA_40)])

    def test_planilha_antiga_bloqueia(self):
        with self.assertRaisesMessage(ValueError, "semana 39"):
            validate_report_is_current([_contrato(SEMANA_39)])

    def test_planilha_sem_datas_bloqueia(self):
        with self.assertRaisesMessage(ValueError, "não tem colunas de data"):
            validate_report_is_current([_contrato([])])

    def test_planilha_mensal_por_loja_sem_marca_bloqueia(self):
        # Formato baixado por engano em 02/10/2026: uma linha por loja, sem marca, 35 dias
        datas = [f"{d:02d}/09/2026" for d in range(1, 31)] + ["01/10/2026", "02/10/2026", "03/10/2026", "04/10/2026"]
        linha = {"ponto_de_venda": "ASSAÍ BAURU (CENTRO) - LJ 061", "frequencia_contratada": 30}
        linha.update({d: 0 for d in datas})
        with self.assertRaisesMessage(ValueError, "não tem a coluna de indústria/marca"):
            validate_report_is_current([linha] * 49)

    def test_planilha_com_maioria_sem_marca_bloqueia(self):
        sem_marca = {**_contrato(SEMANA_40), "industria": ""}
        with self.assertRaisesMessage(ValueError, "2 de 3 linhas"):
            validate_report_is_current([_contrato(SEMANA_40), sem_marca, sem_marca])

    def test_planilha_com_mais_de_uma_semana_bloqueia(self):
        with self.assertRaisesMessage(ValueError, "cobre 14 dias"):
            validate_report_is_current([_contrato(SEMANA_39 + SEMANA_40)])

    def test_motor_bloqueia_planilha_antiga(self):
        with self.assertRaises(ValueError):
            analyze_reschedules([_contrato(SEMANA_39)], [])

    def test_motor_sem_contratos_retorna_vazio(self):
        self.assertEqual(analyze_reschedules([], []), [])


@mock.patch.object(frequency_checker, "datetime", QuintaFeira)
class ContratoNovoMotorTests(SimpleTestCase):
    """URBANO no Assaí, 2x por semana (Segunda e Quarta); hoje é quinta, 01/10."""

    def setUp(self):
        self.media = tempfile.TemporaryDirectory()
        pasta = Path(self.media.name) / "json_exports"
        pasta.mkdir()
        (pasta / "client_visit_schedules.json").write_text(json.dumps([{
            "rede": "ASSAÍ", "industria": "URBANO", "frequencia": "2x por semana",
            "dias_esperados": ["Segunda", "Quarta"],
        }]), encoding="utf-8")
        self.settings_override = override_settings(MEDIA_ROOT=self.media.name)
        self.settings_override.enable()
        self.chave = contract_row_key(_contrato([]))[0]

    def tearDown(self):
        self.settings_override.disable()
        self.media.cleanup()

    def _analisar(self, inicio=None, visitas=None):
        inicio_contratos = {self.chave: inicio} if inicio else None
        return analyze_reschedules([_contrato(SEMANA_40, visitas)], [], inicio_contratos)

    def test_contrato_antigo_cobra_segunda_e_quarta(self):
        (p,) = self._analisar()
        self.assertEqual(p["visitas_esperadas_ate_ontem"], 2)
        self.assertNotIn("situacao", p)

    def test_contrato_que_apareceu_hoje_nao_gera_reagendamento(self):
        (p,) = self._analisar(inicio=date(2026, 10, 1))
        self.assertEqual(p["situacao"], SITUACAO_CONTRATO_NOVO)
        self.assertIn("Contrato novo: apareceu em 01/10/2026", p["log"])

    def test_contrato_de_terca_so_cobra_quarta(self):
        (p,) = self._analisar(inicio=date(2026, 9, 29))
        self.assertEqual(p["visitas_esperadas_ate_ontem"], 1)
        self.assertNotIn("situacao", p)
        self.assertIn("Contrato novo desde 29/09/2026", p["log"])

    def test_contrato_de_terca_com_quarta_visitada_nao_e_reagendado(self):
        (p,) = self._analisar(inicio=date(2026, 9, 29), visitas={"30/09/2026": 1})
        self.assertEqual(p["situacao"], SITUACAO_CONTRATO_NOVO)


class MarcasEquivalentesTests(TestCase):
    def test_tucs_fazendinha_ja_vem_cadastrada_como_bela_vista(self):
        from rules_engine.services.brands import load_brand_equivalents, make_canonical

        equivalentes = load_brand_equivalents()
        self.assertEqual(equivalentes.get("TUCS/FAZENDINHA"), "BELA VISTA")
        canonica = make_canonical(equivalentes)
        self.assertEqual(canonica("Tucs/Fazendinha"), "BELA VISTA")
        self.assertEqual(canonica("MUPY TP"), "MUPY TP")

    def test_liberty_ja_vem_cadastrada_como_liberty_plast(self):
        from rules_engine.services.brands import load_brand_equivalents, make_canonical

        canonica = make_canonical(load_brand_equivalents())
        self.assertEqual(canonica("LIBERTY"), canonica("LIBERTY PLAST"))
        self.assertEqual(canonica("Liberty"), "LIBERTY PLAST")


class HistoricoContratosTests(TestCase):
    QUARTA, QUINTA = date(2026, 9, 30), date(2026, 10, 1)

    def _linha(self, loja, marca="URBANO"):
        return {"ponto_de_venda": loja, "industria": marca}

    def _sincronizar(self, linhas, dia):
        inicio, registro = compute_contract_starts(linhas, dia)
        novos = save_contract_presence(registro)
        return inicio, novos

    def test_primeira_sincronizacao_vira_base_sem_contratos_novos(self):
        inicio, novos = self._sincronizar([self._linha("LOJA A"), self._linha("LOJA B")], self.QUARTA)
        self.assertEqual((inicio, novos), ({}, 0))
        self.assertEqual(ContractPresence.objects.filter(primeiro_dia__isnull=True).count(), 2)

    def test_contrato_que_nao_estava_ontem_e_novo(self):
        self._sincronizar([self._linha("LOJA A")], self.QUARTA)
        inicio, novos = self._sincronizar([self._linha("LOJA A"), self._linha("LOJA B")], self.QUINTA)
        chave_b = contract_row_key(self._linha("LOJA B"))[0]
        self.assertEqual(inicio, {chave_b: self.QUINTA})
        self.assertEqual(novos, 1)

    def test_segunda_sincronizacao_no_mesmo_dia_mantem_o_primeiro_dia(self):
        self._sincronizar([self._linha("LOJA A")], self.QUARTA)
        self._sincronizar([self._linha("LOJA A"), self._linha("LOJA B")], self.QUINTA)
        inicio, novos = self._sincronizar([self._linha("LOJA A"), self._linha("LOJA B")], self.QUINTA)
        self.assertEqual(list(inicio.values()), [self.QUINTA])
        self.assertEqual(novos, 1)

    def test_linha_sem_marca_nao_entra_no_historico(self):
        self._sincronizar([self._linha("LOJA A")], self.QUARTA)
        inicio, novos = self._sincronizar([self._linha("LOJA A"), self._linha("LOJA B", marca="")], self.QUINTA)
        self.assertEqual((inicio, novos), ({}, 0))
        self.assertFalse(ContractPresence.objects.filter(industria="").exists())

    def test_contrato_que_sumiu_e_voltou_recomeca(self):
        self._sincronizar([self._linha("LOJA A"), self._linha("LOJA B")], date(2026, 9, 28))
        self._sincronizar([self._linha("LOJA A")], date(2026, 9, 29))           # B sumiu
        inicio, _ = self._sincronizar([self._linha("LOJA A"), self._linha("LOJA B")], self.QUARTA)
        self.assertEqual(inicio, {contract_row_key(self._linha("LOJA B"))[0]: self.QUARTA})

    def test_contrato_novo_de_semana_anterior_nao_limita_esta_semana(self):
        self._sincronizar([self._linha("LOJA A")], date(2026, 9, 24))
        self._sincronizar([self._linha("LOJA A"), self._linha("LOJA B")], date(2026, 9, 25))  # B novo na semana 39
        inicio, _ = self._sincronizar([self._linha("LOJA A"), self._linha("LOJA B")], self.QUINTA)
        self.assertEqual(inicio, {})
