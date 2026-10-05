# rules_engine/admin.py

from django.contrib import admin

from .models import (
    AtendimentoExtra,
    AtendimentoExtraItem,
    AtividadeIndustria,
    ClientScheduleConfig,
    ContractPresence,
    ExecutionBatch,
    Justificativa,
    MarcaEquivalente,
    RescheduleProposal,
)


class AtendimentoExtraItemInline(admin.TabularInline):
    model = AtendimentoExtraItem
    extra = 0
    fields = ("ponto_de_venda", "promotor_designado", "itinerary_id", "status", "log")
    readonly_fields = fields


@admin.register(AtendimentoExtra)
class AtendimentoExtraAdmin(admin.ModelAdmin):
    list_display = ("data", "marca", "redes", "status", "usuario", "criado_em", "executado_em")
    list_filter = ("status", "data")
    search_fields = ("marca", "motivo")
    inlines = [AtendimentoExtraItemInline]


@admin.register(AtividadeIndustria)
class AtividadeIndustriaAdmin(admin.ModelAdmin):
    list_display = ("survey_schedule_id", "nome", "industria", "atualizado_em")
    list_filter = ("industria",)
    search_fields = ("nome", "industria", "survey_schedule_id")


@admin.register(MarcaEquivalente)
class MarcaEquivalenteAdmin(admin.ModelAdmin):
    list_display = ("nome", "marca")
    search_fields = ("nome", "marca")


@admin.register(Justificativa)
class JustificativaAdmin(admin.ModelAdmin):
    list_display = ("semana", "ponto_de_venda", "industria", "motivo", "usuario", "criado_em")
    list_filter = ("semana",)
    search_fields = ("ponto_de_venda", "industria", "motivo")


@admin.register(ContractPresence)
class ContractPresenceAdmin(admin.ModelAdmin):
    list_display = ("ponto_de_venda", "industria", "primeiro_dia", "ultimo_dia")
    list_filter = ("primeiro_dia", "ultimo_dia")
    search_fields = ("ponto_de_venda", "industria")


@admin.register(RescheduleProposal)
class RescheduleProposalAdmin(admin.ModelAdmin):
    list_display = (
        "data_criacao", "ponto_de_venda", "industria", "supervisor",
        "promotor_designado", "promotor_id", "status",
    )
    list_filter = ("status", "supervisor", "data_criacao")
    search_fields = ("ponto_de_venda", "industria", "promotor_designado", "cnpj")
    date_hierarchy = "data_criacao"


@admin.register(ClientScheduleConfig)
class ClientScheduleConfigAdmin(admin.ModelAdmin):
    list_display = ("rede", "industria", "frequencia_contratada", "dias_validados", "data_atualizacao")
    list_filter = ("rede", "frequencia_contratada")
    search_fields = ("industria",)


@admin.register(ExecutionBatch)
class ExecutionBatchAdmin(admin.ModelAdmin):
    list_display = ("id", "data_inicio", "data_fim", "total_processados", "total_sucesso", "total_erros")
