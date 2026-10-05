# Reagendamento — Automação Puzzle

Painel Django que identifica visitas de merchandising em atraso (Assaí e Atacadão), propõe o
reagendamento e liga a marca na rota do promotor no **Puzzle** (`hipertrade-puzzle-op.techtd.com.br`)
por automação (Playwright + HTTP). Também faz **atendimento extra** (ligar uma marca num dia escolhido).

## Visão geral

| App | Função |
|---|---|
| `scraping` | Acesso ao Puzzle: login, exportação do Indicador de Contratos, rotas fixas, calendário/rota do dia, relatório de operação, ligar marca na rota |
| `rules_engine` | Modelos e regras: visitas esperadas x realizadas, contrato novo, marcas equivalentes, justificativas |
| `dashboard` | Telas: painel de reagendamentos, regras de dias de visita, resumo por supervisor (texto/imagem), atendimento extra |

Fluxo da sincronização (botão **Sincronizar** no painel):
1. Exporta o Indicador de Contratos e espera a notificação **nova** desse relatório no Puzzle; baixa o `.xlsx`.
2. Baixa as rotas fixas (API DataTables do Puzzle).
3. Calcula as propostas (regras de dias da tela *Regras de Dias de Visita*; contrato novo só conta a partir do 1º dia).
4. Consulta a rota de hoje de cada promotor: já agendada, coberta por outro promotor (rota ou tarefa de EXECUÇÃO), rota finalizada, loja fora da rota etc.

A execução liga a marca na rota de hoje (uma vez por promotor, com até 3 tentativas e conferência).

## Requisitos

- Python **3.12+** (testado com 3.14)
- Acesso de rede ao Puzzle
- Navegador do Playwright: `python -m playwright install chromium`
  (em Linux, também `python -m playwright install-deps chromium`)

## Instalação

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    |    Linux: source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium

cp .env.example .env        # preencher (ver abaixo)
python manage.py migrate
python manage.py loaddata dados_iniciais   # regras de dias, marcas equivalentes, histórico de contratos
python manage.py createsuperuser
python manage.py test                      # opcional: suíte automatizada (não acessa o Puzzle)
python manage.py runserver 0.0.0.0:8000
```

### Variáveis do `.env`

| Variável | Descrição |
|---|---|
| `DJANGO_SECRET_KEY` | Chave do Django (gerar uma nova por ambiente) |
| `DJANGO_DEBUG` | `False` em produção |
| `DJANGO_ALLOWED_HOSTS` | Endereços de acesso ao painel, separados por vírgula |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | Se houver HTTPS/proxy: ex. `https://painel.empresa.com.br` |
| `PUZZLE_BASE_URL` | URL do Puzzle |
| `PUZZLE_USER` / `PUZZLE_PASS` | Usuário do robô no Puzzle |
| `PUZZLE_HEADLESS` | `True` em servidor (sem janela do navegador) |

## Pontos de atenção para a integração

- **Sessão única no Puzzle:** um novo login com o mesmo usuário derruba a sessão anterior. Use um
  **usuário exclusivo do robô** e não use esse usuário no navegador enquanto o robô roda.
- **Uma automação por vez:** a trava fica em memória (`threading.Lock` em `dashboard/views.py`).
  Com vários processos (ex.: gunicorn com vários workers) ela não vale entre processos — usar 1 worker
  ou trocar por uma trava compartilhada (banco/Redis).
- **Execuções longas:** sincronização (~1–2 min) e atendimento extra de rede inteira (vários minutos)
  rodam dentro da requisição. Em servidor, aumentar o timeout do proxy/WSGI ou mover para fila (ex.: Celery).
- **Playwright e banco:** o `sync_playwright` mantém um event loop ativo; o ORM do Django não pode ser
  chamado dentro do bloco `with sync_playwright()` (as views já leem/gravam o banco antes/depois).
- **Banco:** SQLite por padrão (`core/settings.py`). Para PostgreSQL/outro, ajustar `DATABASES`.
- **Arquivos gerados** ficam em `MEDIA_ROOT` (`media/`): planilha baixada, JSONs de contratos,
  rotas fixas e regras de dias (`client_visit_schedules.json`, regenerado ao salvar a tela de regras).
- **Cadastros mantidos pelo operador** (em `/admin`): *Marcas Equivalentes* (ex.: TUCS/FAZENDINHA = BELA VISTA,
  LIBERTY = LIBERTY PLAST), *Justificativas*, *Indústrias das Atividades* (cache), *Atendimentos Extras*.

### Integrando como apps dentro de outro projeto Django

Copiar `dashboard/`, `rules_engine/` e `scraping/`, adicioná-los ao `INSTALLED_APPS`, incluir
`dashboard.urls` nas URLs e levar para o `settings.py` do projeto: `PUZZLE_BASE_URL`, `PUZZLE_USER`,
`PUZZLE_PASS`, `PUZZLE_HEADLESS`, `MEDIA_ROOT`, `LOGIN_URL`, `LOGIN_REDIRECT_URL`, `LOGOUT_REDIRECT_URL`,
`MESSAGE_TAGS` e as rotas de login/logout de `core/urls.py`. Depois `migrate` e `loaddata dados_iniciais`.

## Estrutura

```
core/                     settings, urls
dashboard/                views (reagendamento, atendimento extra), templates, testes
rules_engine/             models, migrations, fixtures/dados_iniciais.json, services (regras, marcas, contratos)
scraping/services/        exporter, routes_api, calendar_automation, operation_report, extra_service, puzzle_session
scraping/services/test_*  scripts manuais de diagnóstico (acessam o Puzzle; test_calendar_toggle só salva com --confirmar)
```
