# rules_engine/models.py

from django.conf import settings
from django.db import models


class RescheduleProposal(models.Model):
    STATUS_CHOICES = [
        ('PENDENTE', 'Pendente de Aprovação'),
        ('AGENDADO_HOJE', 'Já agendada hoje no Puzzle'),
        ('COBERTA', 'Coberta por outro promotor'),
        ('ROTA_FINALIZADA', 'Rota de hoje finalizada'),
        ('SEM_ROTA_HOJE', 'Promotor sem rota hoje'),
        ('FORA_DA_ROTA', 'Loja fora da rota de hoje'),
        ('CONTRATO_NOVO', 'Contrato novo'),
        ('JUSTIFICADO', 'Justificado pelo operador'),
        ('APROVADO', 'Aprovado para Execução'),
        ('REJEITADO', 'Rejeitado pelo Operador'),
        ('EXECUTADO', 'Executado no Puzzle'),
        ('ERRO', 'Erro na Execução'),
    ]

    ponto_de_venda = models.CharField(max_length=255, verbose_name="Ponto de Venda")
    cnpj = models.CharField(max_length=20, verbose_name="CNPJ")
    industria = models.CharField(max_length=100, verbose_name="Indústria / Marca")
    supervisor = models.CharField(max_length=150, verbose_name="Supervisor")
    
    dias_contratados = models.CharField(max_length=100, verbose_name="Dias Contratados")
    visitas_esperadas = models.IntegerField(default=0, verbose_name="Visitas Esperadas")
    visitas_esperadas_ate_ontem = models.IntegerField(default=0, verbose_name="Visitas Esperadas até Ontem")
    visitas_realizadas = models.IntegerField(default=0, verbose_name="Visitas Realizadas")
    visitas_pendentes = models.IntegerField(default=1, verbose_name="Visitas Pendentes")
    
    promotor_designado = models.CharField(max_length=150, verbose_name="Promotor")
    promotor_id = models.IntegerField(null=True, blank=True, verbose_name="ID do Promotor (Pearson ID)")
    
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='PENDENTE', verbose_name="Status")
    log_erro = models.TextField(blank=True, null=True, verbose_name="Log de Erro/Sucesso")
    
    data_criacao = models.DateTimeField(auto_now_add=True, verbose_name="Data de Criação")
    data_atualizacao = models.DateTimeField(auto_now=True, verbose_name="Última Atualização")

    class Meta:
        verbose_name = "Proposta de Reagendamento"
        verbose_name_plural = "Propostas de Reagendamento"
        ordering = ['-data_criacao', 'ponto_de_venda']

    def __str__(self):
        return f"{self.ponto_de_venda} - {self.industria} ({self.promotor_designado})"


class ExecutionBatch(models.Model):
    """Mapeia cada lote de execuções disparado pelo operador."""
    
    data_inicio = models.DateTimeField(auto_now_add=True, verbose_name="Início da Execução")
    data_fim = models.DateTimeField(null=True, blank=True, verbose_name="Fim da Execução")
    total_processados = models.IntegerField(default=0, verbose_name="Total Processados")
    total_sucesso = models.IntegerField(default=0, verbose_name="Total com Sucesso")
    total_erros = models.IntegerField(default=0, verbose_name="Total com Erros")

    class Meta:
        verbose_name = "Lote de Execução"
        verbose_name_plural = "Lotes de Execução"

    def __str__(self):
        return f"Lote #{self.id} em {self.data_inicio.strftime('%d/%m/%Y %H:%M')}"


class ContractPresence(models.Model):
    """Contratos (loja + marca) vistos nas planilhas sincronizadas: detecta contrato novo.

    Um contrato que não estava na sincronização anterior ganha primeiro_dia = dia em que
    apareceu; as visitas de antes desse dia não são cobradas.
    """
    chave = models.CharField(max_length=400, unique=True, verbose_name="Chave (loja|marca normalizadas)")
    ponto_de_venda = models.CharField(max_length=255, verbose_name="Ponto de Venda")
    industria = models.CharField(max_length=100, verbose_name="Indústria / Marca")
    primeiro_dia = models.DateField(
        null=True, blank=True,
        verbose_name="Primeiro dia",
        help_text="Vazio = já existia quando o histórico começou a ser registrado.",
    )
    ultimo_dia = models.DateField(verbose_name="Último dia visto")

    class Meta:
        verbose_name = "Presença de Contrato"
        verbose_name_plural = "Presença de Contratos"

    def __str__(self):
        return f"{self.ponto_de_venda} - {self.industria} (desde {self.primeiro_dia or 'antes do histórico'})"


class Justificativa(models.Model):
    """Justificativa do operador (ex.: feriado) para loja + marca, válida até o fim da semana."""
    ponto_de_venda = models.CharField(max_length=255, verbose_name="Ponto de Venda")
    industria = models.CharField(max_length=100, verbose_name="Indústria / Marca")
    cnpj = models.CharField(max_length=20, verbose_name="CNPJ")
    semana = models.DateField(verbose_name="Semana (segunda-feira)")
    motivo = models.TextField(verbose_name="Motivo")
    usuario = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, verbose_name="Operador"
    )
    criado_em = models.DateTimeField(auto_now_add=True, verbose_name="Criado em")

    class Meta:
        verbose_name = "Justificativa"
        verbose_name_plural = "Justificativas"
        unique_together = ('ponto_de_venda', 'industria', 'cnpj', 'semana')

    def __str__(self):
        return f"{self.ponto_de_venda} - {self.industria} (semana {self.semana:%d/%m}): {self.motivo}"


class MarcaEquivalente(models.Model):
    """Nome alternativo de uma marca (ex.: tarefas "TUCS/FAZENDINHA" = marca BELA VISTA).

    Usado ao cruzar as fontes do Puzzle, principalmente o título das tarefas do dia.
    """
    nome = models.CharField(
        max_length=150, unique=True, verbose_name="Nome alternativo",
        help_text='Como aparece no Puzzle (ex.: "TUCS/FAZENDINHA" ou "MONRÉVE BATATA PALHA").',
    )
    marca = models.CharField(
        max_length=100, verbose_name="Marca",
        help_text='Nome da marca como está na planilha de contratos (ex.: "BELA VISTA").',
    )

    class Meta:
        verbose_name = "Marca Equivalente"
        verbose_name_plural = "Marcas Equivalentes"
        ordering = ["marca", "nome"]

    def __str__(self):
        return f"{self.nome} = {self.marca}"


class AtividadeIndustria(models.Model):
    """Cache da indústria cadastrada em cada atividade do Puzzle (/survey_schedules/edit/{id}).

    As tarefas do relatório de operação apontam para a atividade; a indústria cadastrada nela é
    mais confiável que o título (ex.: "LOBIT'S - SHARE" é da MILHO DE OURO).
    """
    survey_schedule_id = models.IntegerField(unique=True, verbose_name="ID da atividade no Puzzle")
    nome = models.CharField(max_length=255, blank=True, verbose_name="Nome da atividade")
    industria = models.CharField(
        max_length=100, blank=True, verbose_name="Indústria",
        help_text="Vazio = a atividade não tem indústria cadastrada no Puzzle.",
    )
    atualizado_em = models.DateTimeField(auto_now=True, verbose_name="Lido do Puzzle em")

    class Meta:
        verbose_name = "Indústria da Atividade"
        verbose_name_plural = "Indústrias das Atividades"
        ordering = ["industria", "nome"]

    def __str__(self):
        return f"{self.nome} ({self.survey_schedule_id}) -> {self.industria or 'sem indústria'}"


class AtendimentoExtra(models.Model):
    """Pedido de atendimento extra: ligar uma marca, num dia, nas lojas de uma rede ou escolhidas.

    A marca é ligada na rota agendada do dia do promotor de rota fixa que faz a marca na loja.
    """
    STATUS_CHOICES = [
        ('PREVIA', 'Prévia gerada'),
        ('EXECUTADO', 'Executado'),
        ('PARCIAL', 'Executado com pendências'),
    ]

    marca = models.CharField(max_length=100, verbose_name="Marca / Indústria")
    data = models.DateField(verbose_name="Dia do atendimento")
    redes = models.CharField(
        max_length=100, blank=True, verbose_name="Redes",
        help_text="Rede inteira escolhida (ex.: ASSAÍ, ATACADÃO); vazio = lojas escolhidas uma a uma.",
    )
    motivo = models.TextField(blank=True, verbose_name="Motivo / Observação")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='PREVIA', verbose_name="Status")
    usuario = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, verbose_name="Operador"
    )
    criado_em = models.DateTimeField(auto_now_add=True, verbose_name="Criado em")
    executado_em = models.DateTimeField(null=True, blank=True, verbose_name="Executado em")

    class Meta:
        verbose_name = "Atendimento Extra"
        verbose_name_plural = "Atendimentos Extras"
        ordering = ['-criado_em']

    def __str__(self):
        return f"{self.marca} em {self.data:%d/%m/%Y} ({self.get_status_display()})"


class AtendimentoExtraItem(models.Model):
    """Uma loja de um pedido de atendimento extra, com a situação apurada na prévia e o resultado."""
    STATUS_CHOICES = [
        ('PRONTO', 'Pronto para ligar'),
        ('JA_ATIVO', 'Marca já ativa'),
        ('SEM_PROMOTOR', 'Sem promotor de rota fixa'),
        ('SEM_ROTA', 'Promotor sem rota no dia'),
        ('FORA_DA_ROTA', 'Loja fora da rota do dia'),
        ('ROTA_FINALIZADA', 'Rota do dia finalizada'),
        ('NAO_VERIFICADO', 'Não verificado'),
        ('EXECUTADO', 'Marca ligada'),
        ('ERRO', 'Erro'),
    ]

    atendimento = models.ForeignKey(AtendimentoExtra, on_delete=models.CASCADE, related_name="itens")
    ponto_de_venda = models.CharField(max_length=255, verbose_name="Ponto de Venda")
    promotor_designado = models.CharField(max_length=150, blank=True, verbose_name="Promotor")
    promotor_id = models.IntegerField(null=True, blank=True, verbose_name="ID do Promotor")
    itinerary_id = models.IntegerField(null=True, blank=True, verbose_name="Itinerário do dia")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='NAO_VERIFICADO', verbose_name="Status")
    log = models.TextField(blank=True, verbose_name="Detalhe")

    class Meta:
        verbose_name = "Loja do Atendimento Extra"
        verbose_name_plural = "Lojas do Atendimento Extra"
        ordering = ['ponto_de_venda']

    def __str__(self):
        return f"{self.ponto_de_venda}: {self.get_status_display()}"


class ClientScheduleConfig(models.Model):
    """Guarda os dias oficiais por Rede, Indústria e Frequência Contratada."""
    rede = models.CharField(max_length=100, verbose_name="Rede / Cliente Principal")
    industria = models.CharField(max_length=100, verbose_name="Indústria / Marca")
    frequencia_contratada = models.CharField(max_length=50, verbose_name="Frequência Contratada")
    dias_validados = models.CharField(max_length=255, verbose_name="Dias Oficiais de Visita")
    data_atualizacao = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Configuração de Agenda do Cliente"
        verbose_name_plural = "Configurações de Agendas dos Clientes"
        # Agora a combinação única considera a Frequência!
        unique_together = ('rede', 'industria', 'frequencia_contratada')

    def __str__(self):
        return f"{self.rede} ({self.industria}) - {self.frequencia_contratada} -> {self.dias_validados}"