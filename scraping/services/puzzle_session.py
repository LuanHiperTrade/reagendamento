# scraping/services/puzzle_session.py

import httpx
from django.conf import settings
from playwright.sync_api import Page

BASE_URL = settings.PUZZLE_BASE_URL

XHR_HEADERS = {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"}
BROWSER_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


def puzzle_login(page: Page) -> None:
    """Autentica no Puzzle com as credenciais do .env (PUZZLE_USER / PUZZLE_PASS).

    ATENÇÃO: o Puzzle mantém UMA sessão por usuário; um novo login derruba a sessão
    anterior (que passa a receber 401). Reaproveite a sessão dentro de um mesmo fluxo.
    """
    # Sem "networkidle": quando o Puzzle está lento ele pode nunca ficar ocioso e travar o robô
    page.goto(f"{BASE_URL}/login", wait_until="domcontentloaded", timeout=60000)
    campo_email = page.locator('input[type="email"], input[name="email"], input[name="username"]').first
    campo_email.wait_for(state="visible", timeout=60000)
    campo_email.fill(settings.PUZZLE_USER)
    page.fill('input[type="password"], input[name="password"]', settings.PUZZLE_PASS)
    page.click('button[type="submit"], input[type="submit"]')
    try:
        page.wait_for_url(lambda url: "/login" not in url, timeout=60000)
    except Exception:
        raise RuntimeError(
            "O login no Puzzle não foi concluído em 60 s (usuário/senha do .env ou Puzzle fora do ar)."
        )
    page.wait_for_load_state("domcontentloaded")


def puzzle_get(client: httpx.Client, url: str, **kwargs) -> httpx.Response:
    """GET no Puzzle com erro claro quando a sessão foi derrubada (401)."""
    resp = client.get(url, **kwargs)
    if resp.status_code == 401:
        raise RuntimeError(
            "A sessão do Puzzle expirou (401). Provavelmente houve outro login com o mesmo"
            f" usuário ({settings.PUZZLE_USER}) enquanto o robô rodava."
        )
    resp.raise_for_status()
    return resp
