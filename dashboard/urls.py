# dashboard/urls.py

from django.urls import path
from . import views, views_extra

urlpatterns = [
    path('', views.reschedule_dashboard, name='reschedule_dashboard'),
    path('run-pipeline/', views.run_pipeline, name='run_pipeline'),
    path('execute-selected/', views.execute_selected_reschedules, name='execute_selected'),
    path('justify-selected/', views.justify_selected, name='justify_selected'),
    # Atendimento extra: ligar uma marca num dia, nas lojas de uma rede ou escolhidas
    path('atendimento-extra/', views_extra.extra_form, name='extra_form'),
    path('atendimento-extra/lojas/', views_extra.extra_stores, name='extra_stores'),
    path('atendimento-extra/previa/', views_extra.extra_preview, name='extra_preview'),
    path('atendimento-extra/<int:pk>/', views_extra.extra_detail, name='extra_detail'),
    path('atendimento-extra/<int:pk>/executar/', views_extra.extra_execute, name='extra_execute'),
    path('resumo-supervisor/', views.supervisor_summary, name='supervisor_summary'),
    path('resumo-supervisor/imagem/', views.supervisor_summary_image, name='supervisor_summary_image'),
    # Novas rotas de Auditoria de Frequências
    path('audit/', views.audit_dashboard, name='audit_dashboard'),
    path('save-schedule/', views.save_client_schedule, name='save_client_schedule'),
]