# scraping/tests.py

import json
import re
from collections import Counter
from datetime import datetime as real_datetime
from unittest import mock

from django.test import SimpleTestCase

from scraping.services import calendar_automation as ca
from scraping.services import operation_report
from scraping.services.puzzle_session import puzzle_get


class FakeDatetime(real_datetime):
    """Fixa 'hoje' em quarta, 30/09/2026."""
    @classmethod
    def now(cls, tz=None):
        return real_datetime(2026, 9, 30, 6, 30)


TABOAO = "ASSAÍ TABOÃO DA SERRA (PIRAJUSSARA) - LJ 093"
SONIA = "ASSAÍ SÃO PAULO (VL. SÔNIA) - LJ 008"
DIADEMA = "ASSAÍ DIADEMA (PIRAPORINHA) - LJ 1"


def _loja(sale_point_id, nome, ativas):
    contratos = [{"id": 1000 + n, "industry": marca, "disabled_route": False} for n, marca in enumerate(ativas)]
    return f"var li = ToDoKm.Routes.SalesPoint2Li( {json.dumps({'id': sale_point_id, 'name': nome, 'contracts': contratos})} );"


def _edit_html(*lojas):
    return "\n".join([
        "<script>function add(sp){ return ToDoKm.Routes.SalesPoint2Li(sp); }</script>",  # chamada sem JSON
        "<script>", *lojas, "</script>",
    ])


def _evento(itinerary_id, data, lojas, editavel=True):
    lis = "".join(f'<li class="" title="{loja}" data-toggle="tooltip"> {loja}</li>' for loja in lojas)
    acoes = f"<a title='Editar Rota' href='https://x/itineraries/edit/{itinerary_id}'></a>" if editavel else "*Rota finalizada"
    return {"id": itinerary_id, "title": f"<p>Rota dia {data} <br>Semana 2</p><ul>{lis}</ul>{acoes}"}


class FakePuzzle:
    """Cliente HTTP falso: responde calendário por promotor e tela de edição por itinerário."""

    def __init__(self, calendarios, telas, atividades=None):
        self.calendarios, self.telas, self.urls = calendarios, telas, []
        self.atividades = atividades or {}   # {id da atividade: indústria cadastrada}

    def get(self, url, **kw):
        self.urls.append(url)
        resp = mock.Mock(status_code=200)
        resp.raise_for_status = lambda: None
        if m := re.search(r"/calendar/list/(\d+)", url):
            resp.json = lambda: self.calendarios.get(int(m.group(1)), [])
        elif m := re.search(r"/itineraries/edit/(\d+)", url):
            resp.text = self.telas[int(m.group(1))]
        elif m := re.search(r"/survey_schedules/edit/(\d+)", url):
            resp.text = _atividade_html(self.atividades.get(int(m.group(1))))
        return resp


def _atividade_html(industria):
    """Tela de edição da atividade com a indústria selecionada (ou nenhuma)."""
    opcoes = '<option value="">Selecione</option><option value="7">OUTRA</option>'
    if industria:
        opcoes += f'<option value="42" selected="selected">{industria}</option>'
    return f'<form><select class="form-control" name="industry_id" id="industry_id">{opcoes}</select></form>'


def _item(loja, marca, promotor_id=18337):
    return {"cliente": loja, "industria": marca, "promotor_id": promotor_id}


class JsonEmbutidoTests(SimpleTestCase):
    def test_le_marcas_ativas_por_loja_e_ignora_chamada_sem_json(self):
        html = _edit_html(_loja(11211, TABOAO, ["QUELUZ", "COCO &amp; CIA"]), _loja(10929, SONIA, ["PRATS"]))
        self.assertEqual(ca.parse_active_contracts(html), {
            ca.normalize_text(TABOAO): {"QUELUZ", "COCO CIA"},
            ca.normalize_text(SONIA): {"PRATS"},
        })


@mock.patch.object(ca, "datetime", FakeDatetime)
class ClassificacaoTests(SimpleTestCase):
    def _classificar(self, calendarios, telas, itens, promoter_id=18337):
        client = FakePuzzle(calendarios, telas)
        situacoes = ca._classify_promoter(ca.RotasDeHoje(client), promoter_id, itens)
        return {(i["cliente"], i["industria"]): situacoes[id(i)][0] for i in itens}, client

    def test_rota_aberta_separa_ativa_desligada_e_fora_da_rota(self):
        itens = [_item(TABOAO, "QUELUZ"), _item(TABOAO, "COCO & CIA"), _item(TABOAO, "JURERE"), _item(DIADEMA, "ITABOM")]
        situacoes, client = self._classificar(
            {18337: [_evento(774227, "30/09/2026", [TABOAO, SONIA]), _evento(783593, "01/10/2026", [TABOAO])]},
            {774227: _edit_html(_loja(11211, TABOAO, ["QUELUZ", "COCO &amp; CIA"]))},
            itens,
        )
        self.assertEqual(situacoes, {
            (TABOAO, "QUELUZ"): ca.SITUACAO_AGENDADA,
            (TABOAO, "COCO & CIA"): ca.SITUACAO_AGENDADA,
            (TABOAO, "JURERE"): ca.SITUACAO_A_REAGENDAR,
            (DIADEMA, "ITABOM"): ca.SITUACAO_FORA_DA_ROTA,
        })
        # Um calendário + uma tela de edição (a de hoje), mesmo com 4 propostas
        self.assertEqual(len(client.urls), 2)
        self.assertTrue(client.urls[1].endswith("/itineraries/edit/774227"))

    def test_marca_com_outro_nome_na_tela_da_rota_e_reconhecida(self):
        # A tela de edição mostra "LIBERTY"; na planilha a marca é "LIBERTY PLAST"
        from rules_engine.services.brands import make_canonical

        client = FakePuzzle(
            {18337: [_evento(774227, "30/09/2026", [TABOAO])]},
            {774227: _edit_html(_loja(11211, TABOAO, ["LIBERTY"]))},
        )
        item = _item(TABOAO, "LIBERTY PLAST")
        sem = ca._classify_promoter(ca.RotasDeHoje(client), 18337, [item])[id(item)][0]
        com = ca._classify_promoter(
            ca.RotasDeHoje(client, make_canonical({"LIBERTY": "LIBERTY PLAST"})), 18337, [item]
        )[id(item)][0]
        self.assertEqual((sem, com), (ca.SITUACAO_A_REAGENDAR, ca.SITUACAO_AGENDADA))

    def test_rota_finalizada_nao_abre_tela_de_edicao(self):
        situacoes, client = self._classificar(
            {18337: [_evento(773105, "30/09/2026", [TABOAO], editavel=False)]}, {},
            [_item(TABOAO, "QUELUZ"), _item(TABOAO, "JURERE")],
        )
        self.assertEqual(set(situacoes.values()), {ca.SITUACAO_FINALIZADA})
        self.assertEqual(len(client.urls), 1)

    def test_sem_rota_hoje(self):
        situacoes, client = self._classificar(
            {18337: [_evento(783593, "01/10/2026", [TABOAO])]}, {}, [_item(TABOAO, "QUELUZ")]
        )
        self.assertEqual(set(situacoes.values()), {ca.SITUACAO_SEM_ROTA})
        self.assertEqual(len(client.urls), 1)


@mock.patch.object(ca, "datetime", FakeDatetime)
class CoberturaTests(SimpleTestCase):
    """Promotor 18337 é o designado; 12288 (rota aberta) e 12213 (finalizada) estão na mesma loja."""

    def setUp(self):
        self.client = FakePuzzle(
            calendarios={
                18337: [_evento(774227, "30/09/2026", [TABOAO])],
                12288: [_evento(752833, "30/09/2026", [TABOAO, SONIA])],
                12213: [_evento(773105, "30/09/2026", [TABOAO], editavel=False)],
            },
            telas={
                774227: _edit_html(_loja(11211, TABOAO, ["QUELUZ"])),
                752833: _edit_html(_loja(11211, TABOAO, ["JURERE"]), _loja(10929, SONIA, ["PRATS"])),
            },
        )
        self.rotas = ca.RotasDeHoje(self.client)
        self.operacao = {
            "promotores": {
                ca.normalize_text(TABOAO): {18337: "ELIANE", 12288: "RICLEI", 12213: "MARCOS"},
                ca.normalize_text(SONIA): {12288: "RICLEI"},
            },
            "tarefas": {
                ca.normalize_text(TABOAO): [
                    (12213, "MARCOS", "AJA QUIMICA - FOTOS (EXECUÇÃO)", 9001),   # rota finalizada, mas com tarefa
                    (12213, "MARCOS", "MONREVE MP - FOTOS (EXECUÇÃO)", 9002),
                    (18337, "ELIANE", "FREECO - FOTOS (EXECUÇÃO)", 9003),               # do próprio designado
                ],
            },
        }
        self.marcas = operation_report.build_brand_matcher(
            ["AJA QUIMICA", "MONRÉVE", "MONRÉVE MP", "FREECO", "JURERE", "USIBRÁS", "PRATS", "QUELUZ"]
        )

    def _rodar(self, propostas):
        for p in propostas:
            if p.get("promotor_id"):
                s = ca._classify_promoter(self.rotas, p["promotor_id"], [p])[id(p)]
                p["situacao"], p["log"] = s
            else:
                p["situacao"] = ca.SITUACAO_NAO_VERIFICADA
        candidatas = [p for p in propostas if p["situacao"] != ca.SITUACAO_AGENDADA]
        self.lidas = {}
        ca.check_coverage_by_others(
            self.rotas, candidatas, self.operacao, self.marcas, getattr(self, "cache", {}), self.lidas
        )
        return propostas

    def _tarefa(self, pid, nome, titulo, id_atividade, industria):
        """Acrescenta uma tarefa na loja do Taboão cuja atividade tem a indústria informada."""
        self.operacao["tarefas"][ca.normalize_text(TABOAO)].append((pid, nome, titulo, id_atividade))
        self.client.atividades[id_atividade] = industria

    def test_marca_da_tarefa_vem_da_industria_cadastrada_na_atividade(self):
        # "LOBIT'S FOTOS - ANTES E DEPOIS ASSAÍ (EXECUÇÃO)" não tem a marca no título; a atividade é da MILHO DE OURO
        self._tarefa(12213, "MARCOS", "LOBIT'S FOTOS - ANTES E DEPOIS ASSAÍ (EXECUÇÃO)", 8484, "MILHO DE OURO")
        (resultado,) = self._rodar([_item(TABOAO, "MILHO DE OURO")])
        self.assertEqual(resultado["situacao"], ca.SITUACAO_COBERTA)
        self.assertEqual(self.lidas[8484], ("LOBIT'S FOTOS - ANTES E DEPOIS ASSAÍ (EXECUÇÃO)", "MILHO DE OURO"))
        # Atividade sem indústria cadastrada é gravada vazia (e relida na próxima sincronização)
        self.assertEqual(self.lidas[9001], ("AJA QUIMICA - FOTOS (EXECUÇÃO)", ""))

    def test_so_tarefa_de_execucao_conta_como_cobertura(self):
        # GIROS / RUPTURA / PESQUISA não comprovam o atendimento: não cobrem e nem são lidas no Puzzle
        self._tarefa(12213, "MARCOS", "JURERE - GIROS", 7001, "JURERE")
        self._tarefa(12213, "MARCOS", "USIBRAS - PESQUISA ESTOQUE (Estoque)", 7002, "USIBRÁS")
        jurere, usibras = self._rodar([_item(TABOAO, "JURERE"), _item(TABOAO, "USIBRÁS")])
        # JURERE continua coberta, mas pela rota do 12288 (marca ligada), não pela tarefa de GIROS
        self.assertIn("marca ligada na rota de hoje", jurere["log"])
        self.assertEqual(usibras["situacao"], ca.SITUACAO_A_REAGENDAR)
        self.assertFalse(any(u.endswith(("/survey_schedules/edit/7001", "/survey_schedules/edit/7002"))
                             for u in self.client.urls))

    def test_atividade_da_marca_propria_nao_cobre_a_monreve(self):
        # Pelo título ("MONREVE MARCA PRÓPRIA") seria MONRÉVE; a atividade diz MONRÉVE MP
        self._tarefa(12213, "MARCOS", "MONREVE MARCA PRÓPRIA - FOTOS ANTES E DEPOIS (EXECUÇÃO)", 8350, "MONREVE MP")
        monreve, monreve_mp = self._rodar([_item(TABOAO, "MONRÉVE"), _item(TABOAO, "MONRÉVE MP")])
        self.assertEqual(monreve["situacao"], ca.SITUACAO_A_REAGENDAR)
        self.assertEqual(monreve_mp["situacao"], ca.SITUACAO_COBERTA)

    def test_atividade_ja_no_cache_nao_e_lida_de_novo(self):
        self._tarefa(12213, "MARCOS", "LOBIT'S FOTOS - ANTES E DEPOIS ASSAÍ (EXECUÇÃO)", 8484, "MILHO DE OURO")
        self.cache = {8484: "MILHO DE OURO"}
        (resultado,) = self._rodar([_item(TABOAO, "MILHO DE OURO")])
        self.assertEqual(resultado["situacao"], ca.SITUACAO_COBERTA)
        self.assertFalse(any("/survey_schedules/edit/8484" in u for u in self.client.urls))
        self.assertNotIn(8484, self.lidas)

    def test_tarefa_da_marca_com_outro_promotor_vira_coberta_mesmo_com_rota_finalizada(self):
        (resultado,) = self._rodar([_item(TABOAO, "AJA QUIMICA")])
        self.assertEqual(resultado["situacao"], ca.SITUACAO_COBERTA)
        self.assertIn('Coberta por MARCOS (promotor 12213): tarefa "AJA QUIMICA - FOTOS (EXECUÇÃO)"', resultado["log"])

    def test_tarefa_de_marca_mais_longa_nao_cobre_a_mais_curta(self):
        # "MONREVE MP - VENCIMENTO" é da MONRÉVE MP, não da MONRÉVE
        monreve, monreve_mp = self._rodar([_item(TABOAO, "MONRÉVE"), _item(TABOAO, "MONRÉVE MP")])
        self.assertEqual(monreve["situacao"], ca.SITUACAO_A_REAGENDAR)
        self.assertEqual(monreve_mp["situacao"], ca.SITUACAO_COBERTA)

    def test_tarefa_com_nome_equivalente_cobre_a_marca(self):
        # Tarefa "TUCS/FAZENDINHA" cobre a proposta da BELA VISTA
        self.operacao["tarefas"][ca.normalize_text(TABOAO)].append(
            (12213, "MARCOS", "TUCS/FAZENDINHA - FOTOS ANTES E DEPOIS ASSAÍ (EXECUÇÃO)", 6620)
        )
        self.marcas = operation_report.build_brand_matcher(
            ["BELA VISTA", "JURERE"], {"TUCS/FAZENDINHA": "BELA VISTA"}
        )
        (resultado,) = self._rodar([_item(TABOAO, "BELA VISTA")])
        self.assertEqual(resultado["situacao"], ca.SITUACAO_COBERTA)
        self.assertIn('tarefa "TUCS/FAZENDINHA - FOTOS ANTES E DEPOIS ASSAÍ (EXECUÇÃO)"', resultado["log"])

    def test_tarefa_do_proprio_promotor_designado_nao_conta_como_cobertura(self):
        (resultado,) = self._rodar([_item(TABOAO, "FREECO")])
        self.assertEqual(resultado["situacao"], ca.SITUACAO_A_REAGENDAR)

    def test_marca_ligada_na_rota_de_outro_promotor_vira_coberta(self):
        jurere = _item(TABOAO, "JURERE")          # desligada no designado, ligada no 12288
        (resultado,) = self._rodar([jurere])
        self.assertEqual(resultado["situacao"], ca.SITUACAO_COBERTA)
        self.assertIn("Coberta por RICLEI (promotor 12288)", resultado["log"])
        self.assertIn("itinerário 752833", resultado["log"])

    def test_sem_confirmacao_da_marca_nao_vira_coberta_e_anota_rota_finalizada(self):
        usibras = _item(TABOAO, "USIBRÁS")        # ninguém com a marca ligada; 12213 finalizou a rota
        (resultado,) = self._rodar([usibras])
        self.assertEqual(resultado["situacao"], ca.SITUACAO_A_REAGENDAR)
        self.assertIn("MARCOS esteve(estiveram) na loja hoje (rota finalizada)", resultado["log"])

    def test_proposta_sem_promotor_tambem_e_verificada(self):
        prats = _item(SONIA, "PRATS", promotor_id=None)
        (resultado,) = self._rodar([prats])
        self.assertEqual(resultado["situacao"], ca.SITUACAO_COBERTA)

    def test_ja_agendada_pelo_designado_nao_consulta_outros(self):
        queluz = _item(TABOAO, "QUELUZ")
        (resultado,) = self._rodar([queluz])
        self.assertEqual(resultado["situacao"], ca.SITUACAO_AGENDADA)
        self.assertFalse(any("/calendar/list/12288" in u for u in self.client.urls))

    def test_cada_promotor_e_lido_uma_unica_vez(self):
        self._rodar([_item(TABOAO, "JURERE"), _item(TABOAO, "USIBRÁS"), _item(SONIA, "PRATS", promotor_id=None)])
        calendarios = Counter(u for u in self.client.urls if "/calendar/list/" in u)
        telas = Counter(u for u in self.client.urls if "/itineraries/edit/" in u)
        self.assertTrue(all(n == 1 for n in calendarios.values()), calendarios)
        self.assertTrue(all(n == 1 for n in telas.values()), telas)


class RelatorioOperacaoTests(SimpleTestCase):
    def test_le_promotor_lojas_e_tarefas_do_dia(self):
        # Mesmo formato do HTML cru devolvido por /reports/promoters/get-data
        card = (
            '<div class="col-md-6 col-lg-6 col-xl-4"><div class="card"><h5 class="card-title text-center">'
            ' RICLEI VIEIRA SOARES <div class="card-actions"><a href="https://x/itineraries/calendar/12288">'
            '</a></div></h5><div class="collapse" id="collapse12288"><table><tbody><tr><td>1</td><td>'
            f'<a href="https://x/sales-points/show-html/11211" target="_blank" title="{TABOAO}" data-toggle="tooltip">'
            'x</a></td></tr></tbody></table></div>'
            '<div class="collapse" id="collapse12288_tasks"><table><tbody><tr><td>'
            f'<a href="https://x/sales-points/show-html/11211" target="_blank" title="{TABOAO}" data-toggle="tooltip">'
            '<i class="fas fa-store"></i></a></td><td>'
            '<a href="https://x/survey_schedules/edit/6818" target="_blank" title="AGRO FOODS - VENCIMENTO/ESTOQUE"'
            ' data-toggle="tooltip">AGRO FOODS - VENC...</a></td><td>'
            '<a href="https://x/survey_responses/calendar/4764867/complete">2026-09-29</a></td><td></td>'
            '</tr></tbody></table></div></div></div>'
        )
        self.assertEqual(operation_report.parse_operation_cards(card), [{
            "pearson_id": 12288,
            "nome": "RICLEI VIEIRA SOARES",
            "lojas": [TABOAO],
            "tarefas": [(TABOAO, "AGRO FOODS - VENCIMENTO/ESTOQUE", 6818)],
        }])

    def test_tarefa_de_execucao(self):
        for titulo in [
            "AJA QUIMICA - FOTOS (EXECUÇÃO)",
            "MUPY BAG - FOTOS(Execução)",
            "UPA BEBE FOTOS - ANTES E DEPOIS ((EXECUÇÃO)",
            "CORINGA FOTOS (Execucao) ",
            "TUCS/FAZENDINHA - FOTOS ANTES E DEPOIS ASSAÍ (EXECUÇÃO)",
        ]:
            self.assertTrue(operation_report.is_execution_task(titulo), titulo)
        for titulo in [
            "COCO & CIA - GIROS",
            "SANTA EDWIGES - RUPTURA",
            "MONRÉVE - PESQUISA DE PREÇO",
            "ÁPYCE - ANTES E DEPOIS (EXECUÇÃO) - TEMPORARIO",   # (EXECUÇÃO) não está no final
            "LOBIT'S - 2º ATENDIMENTO",
            "",
        ]:
            self.assertFalse(operation_report.is_execution_task(titulo), titulo)

    def test_le_industria_da_atividade(self):
        client = FakePuzzle({}, {}, atividades={6818: "AGROFOODS"})
        self.assertEqual(operation_report.fetch_activity_industry(client, 6818), "AGROFOODS")
        self.assertEqual(operation_report.fetch_activity_industry(client, 1), "")

    def test_marcas_que_podem_ser_confundidas(self):
        marcas = ["MUPY BAG", "MUPY TP", "MONRÉVE", "MONRÉVE MP", "BELA VISTA", "PRATS", "PRATS COMPARTILHADO"]
        marca = operation_report.build_brand_matcher(marcas, {"TUCS/FAZENDINHA": "BELA VISTA"})

        # MUPY BAG x MUPY TP: nunca se misturam; "MUPY" sozinho não é de nenhuma
        self.assertEqual(marca("MUPY BAG - VENCIMENTO"), "MUPY BAG")
        self.assertEqual(marca("MUPY TP - RUPTURA"), "MUPY TP")
        self.assertIsNone(marca("MUPY - PESQUISA"))

        # MONRÉVE x MONRÉVE MP (marca própria)
        self.assertEqual(marca("MONRÉVE - PESQUISA DE PREÇO"), "MONREVE")
        self.assertEqual(marca("MONRÉVE MP - VENCIMENTO"), "MONREVE MP")
        self.assertEqual(marca("MONREVE MP FOTOS"), "MONREVE MP")
        # Linha de produto sem o separador: ambíguo (pode ser da MP) -> não conta
        self.assertIsNone(marca("MONRÉVE BATATA PALHA - FOTOS (EXECUÇÃO)"))
        self.assertIsNone(marca("MONRÉVE CAMPANHA - ASSAÍ"))

        # TUCS/FAZENDINHA = BELA VISTA (cadastro de marcas equivalentes)
        self.assertEqual(marca("TUCS/FAZENDINHA - FOTOS ANTES E DEPOIS ASSAÍ (EXECUÇÃO)"), "BELA VISTA")

        # PRATS x PRATS COMPARTILHADO
        self.assertEqual(marca("PRATS - FOTOS"), "PRATS")
        self.assertEqual(marca("PRATS COMPARTILHADO - FOTOS"), "PRATS COMPARTILHADO")

    def test_liberty_e_liberty_plast_sao_a_mesma_marca(self):
        marca = operation_report.build_brand_matcher(["LIBERTY PLAST", "MUPY BAG"], {"LIBERTY": "LIBERTY PLAST"})
        self.assertEqual(marca("LIBERTY - FOTOS (EXECUÇÃO)"), "LIBERTY PLAST")
        # Sem separador também vale: os dois nomes são da mesma marca, não é "família ambígua"
        self.assertEqual(marca("LIBERTY FOTOS (EXECUÇÃO)"), "LIBERTY PLAST")
        self.assertEqual(marca("LIBERTY PLAST - RUPTURA"), "LIBERTY PLAST")

    def test_linha_de_produto_cadastrada_como_equivalente_passa_a_contar(self):
        marca = operation_report.build_brand_matcher(
            ["MONRÉVE", "MONRÉVE MP"], {"MONRÉVE BATATA PALHA": "MONRÉVE"}
        )
        self.assertEqual(marca("MONRÉVE BATATA PALHA - FOTOS (EXECUÇÃO)"), "MONREVE")
        self.assertIsNone(marca("MONRÉVE CAMPANHA - ASSAÍ"))

    def test_marca_da_tarefa_ignora_acentos_e_espacos(self):
        marca = operation_report.build_brand_matcher(["AGROFOODS", "USIBRÁS", "COCO & CIA", "ALLES"])
        self.assertEqual(marca("AGRO FOODS FOTOS"), "AGROFOODS")
        self.assertEqual(marca("USIBRAS - VENCIMENTO"), "USIBRAS")
        self.assertEqual(marca("COCO &amp; CIA - RUPTURA"), "COCO CIA")
        self.assertEqual(marca("ALLES FOTOS ''CHULETÃO''"), "ALLES")
        self.assertIsNone(marca("APOIO PROMOTOR"))
        self.assertIsNone(marca("FOTOS ANTES E DEPOIS"))


class ContratoNaTelaDeEdicaoTests(SimpleTestCase):
    """Execução: localiza o contrato na tela de edição (Chromium real, página montada offline)."""

    HTML = (
        '<ol class="dd-list"><li class="dd-item dd3-item" data-id="11211"><div class="dd3-content">'
        f'{TABOAO}<br><small id="contract_555"><ul><li>'
        '<a class="button remove d-none"></a><a class="button reEnable"></a> LIBERTY</li></ul></small>'
        '<small id="contract_556"><ul><li><a class="button remove"></a><a class="button reEnable d-none"></a>'
        ' MUPY TP</li></ul></small></div></li></ol>'
    )

    def test_acha_o_contrato_pelo_nome_equivalente(self):
        from playwright.sync_api import sync_playwright
        from rules_engine.services.brands import make_canonical

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content(self.HTML)
                sem, motivo = ca._find_contract(page, TABOAO, "LIBERTY PLAST")
                com, _ = ca._find_contract(page, TABOAO, "LIBERTY PLAST", make_canonical({"LIBERTY": "LIBERTY PLAST"}))
                self.assertIsNone(sem)
                self.assertIn("não tem contrato", motivo)
                self.assertEqual(com.get_attribute("id"), "contract_555")
                self.assertFalse(ca._is_contract_active(com))
            finally:
                browser.close()


class SalvarRotaComTentativasTests(SimpleTestCase):
    """Ligar a marca e salvar, com o Chromium real e um Puzzle simulado (page.route) que pode
    "perder" as primeiras gravações — como aconteceu com a ÁPYCE no itinerário 780761."""

    ITIN = 780761

    def _tela(self, ativo):
        reenable = "button reEnable d-none" if ativo else "button reEnable"
        oculto = '<input type="hidden" name="contract_id[555]" value="555">' if ativo else ""
        return f"""<html><body>
            <form method="POST" action="/itineraries/update/{self.ITIN}" id="form-itinerary">{oculto}
              <ol><li class="dd-item dd3-item" data-id="11211"><div class="dd3-content">{TABOAO}<br>
                <small id="contract_555"><ul><li><a class="button remove">-</a><a class="{reenable}">+</a> ÁPYCE</li></ul></small>
              </div></li></ol>
              <button type="submit" id="btn-submit">Salvar</button>
            </form>
            <script>
              document.querySelector('a.reEnable').addEventListener('click', function () {{
                this.classList.add('d-none');
                document.getElementById('form-itinerary').insertAdjacentHTML(
                  'afterbegin', '<input type="hidden" name="contract_id[555]" value="555">');
              }});
            </script></body></html>"""

    def _executar(self, gravacoes_perdidas):
        from playwright.sync_api import sync_playwright
        from scraping.services.puzzle_session import BASE_URL

        estado = {"ativo": False, "posts": 0}

        def puzzle(route):
            req = route.request
            if req.method == "POST" and f"/itineraries/update/{self.ITIN}" in req.url:
                estado["posts"] += 1
                if estado["posts"] > gravacoes_perdidas and "contract_id%5B555%5D" in (req.post_data or ""):
                    estado["ativo"] = True
                route.fulfill(status=200, content_type="text/html; charset=utf-8", body="<html><body>Rota salva</body></html>")
            elif f"/itineraries/edit/{self.ITIN}" in req.url:
                route.fulfill(status=200, content_type="text/html; charset=utf-8", body=self._tela(estado["ativo"]))
            else:
                route.abort()

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                context = browser.new_context()
                context.route(f"{BASE_URL}/**", puzzle)
                with mock.patch("builtins.print"):
                    r = ca.activate_brands_in_itinerary(
                        context, self.ITIN, [{"id": 1, "industria": "ÁPYCE", "ponto_de_venda": TABOAO}], quando="de 03/10/2026"
                    )
            finally:
                browser.close()
        return r[1], estado

    def test_salva_de_primeira(self):
        r, estado = self._executar(gravacoes_perdidas=0)
        self.assertEqual((r["status"], estado["posts"]), ("success", 1))
        self.assertEqual(r["message"], "Marca 'ÁPYCE' ativada na rota de 03/10/2026 (itinerário 780761).")

    def test_salva_na_segunda_tentativa(self):
        r, estado = self._executar(gravacoes_perdidas=1)
        self.assertEqual((r["status"], estado["posts"]), ("success", 2))
        self.assertIn("na 2ª tentativa", r["message"])

    def test_desiste_depois_de_tres_tentativas_e_segue(self):
        r, estado = self._executar(gravacoes_perdidas=99)
        self.assertEqual((r["status"], estado["posts"]), ("error", 3))
        self.assertIn("não foi confirmado após 3 tentativas", r["message"])


def _notificacao(uid, texto, quando, link=None):
    """Item no mesmo formato do HTML de /notification/list."""
    a = f'<small> <a href="{link}">Clique aqui para baixar o arquivo</a> </small>' if link else ""
    return (
        f'<li class="list-group-item justify-content-between align-items-center notification_item" id="{uid}">'
        '<div class="row"><div class="col-1 p-0"><span class="badge badge-primary ml-2 mt-3 p-2 rounded-circle"></span></div>'
        f'<div class="col-10"><p class="mb-0"> {texto} <br />'
        f'<small style="color: #737373; font-size: 11px;"> {quando} </small> </p> {a} </div></div></li>'
    )


class NotificacoesExportacaoTests(SimpleTestCase):
    """Situação real de 05/10/2026: a 1ª notificação com link era o relatório de rotas fixas de um promotor."""

    LISTA = (
        '<ul class="list-group">'
        + _notificacao("a1", "Finalizado <br /> Geração de Rotas", "05/10/2026 09:54")
        + _notificacao("b2", "Finalizado <br /> Geração de Relatorios de Rotas Fixas do - WILTON DA CONCEICAO",
                       "05/10/2026 09:54", "https://s3.exemplo/xlsx/b2/WILTON.xlsx?a=1&amp;b=2")
        + _notificacao("c3", "Concluído! <br /> Geração Relatorio de Indicador Contratos - Relatórios Indicador de Contratos.",
                       "05/10/2026 09:32", "https://s3.exemplo/xlsx/c3/Indicador%20de%20contratos.xlsx")
        + "</ul>"
    )

    def test_le_id_texto_hora_e_link(self):
        from scraping.services.exporter import parse_notifications

        itens = parse_notifications(self.LISTA)
        self.assertEqual([i["id"] for i in itens], ["a1", "b2", "c3"])
        self.assertEqual(itens[1]["link"], "https://s3.exemplo/xlsx/b2/WILTON.xlsx?a=1&b=2")
        self.assertIsNone(itens[0]["link"])
        self.assertEqual(itens[2]["quando"], real_datetime(2026, 10, 5, 9, 32))
        self.assertIn("Indicador Contratos", itens[2]["texto"])

    def _escolher(self, lista, anteriores=(), pedido=real_datetime(2026, 10, 5, 9, 30)):
        from scraping.services.exporter import parse_notifications, pick_new_contracts_report
        return pick_new_contracts_report(parse_notifications(lista), set(anteriores), pedido)

    def test_ignora_outros_relatorios_e_pega_o_indicador_de_contratos(self):
        escolhida = self._escolher(self.LISTA)
        self.assertEqual(escolhida["id"], "c3")   # não o "WILTON" (rotas fixas), que vem antes

    def test_ignora_notificacao_que_ja_existia_antes_do_pedido(self):
        self.assertIsNone(self._escolher(self.LISTA, anteriores={"a1", "b2", "c3"}))

    def test_ignora_indicador_antigo_e_espera_o_arquivo_ficar_pronto(self):
        # Pedido às 10:00: o indicador das 09:32 é antigo; o novo ainda está sem link (gerando)
        gerando = _notificacao("d4", "Processando <br /> Geração Relatorio de Indicador Contratos", "05/10/2026 10:00")
        lista = self.LISTA.replace("</ul>", gerando + "</ul>")
        self.assertIsNone(self._escolher(lista, pedido=real_datetime(2026, 10, 5, 10, 0, 20)))
        pronto = lista.replace(gerando, _notificacao("d4", "Concluído! <br /> Geração Relatorio de Indicador Contratos",
                                                       "05/10/2026 10:01", "https://s3.exemplo/xlsx/d4/Indicador.xlsx"))
        self.assertEqual(self._escolher(pronto, pedido=real_datetime(2026, 10, 5, 10, 0, 20))["id"], "d4")


class SessaoTests(SimpleTestCase):
    def test_401_gera_mensagem_clara_de_sessao_derrubada(self):
        client = mock.Mock()
        client.get.return_value = mock.Mock(status_code=401)
        with self.assertRaisesMessage(RuntimeError, "outro login com o mesmo"):
            puzzle_get(client, "https://x/itineraries/calendar/list/1")
