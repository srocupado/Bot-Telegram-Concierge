"""Quais marcadores separam 'listagem (dia sem edição)' de 'login/manutenção'."""
import httpx
from bot.config import settings
from bot.services.dou_monitor import _HEADERS, INLABS_BASE

s = settings.inlabs_password.get_secret_value() if settings.inlabs_password else None
MARCAS = ["Sair", "Minha Conta", "Tamanho", "Modificado", "type=\"password\"",
          "logar.php", "manuten", "2026-08-01", "2026-07-31", "DO1.zip"]

with httpx.Client(headers=_HEADERS, follow_redirects=True) as c:
    c.post(f"{INLABS_BASE}/logar.php", data={"email": settings.inlabs_email, "password": s},
           headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=20.0)
    ck = c.cookies.get("inlabs_session_cookie")

    def corpo(url, cookie=True):
        h = {"Cookie": f"inlabs_session_cookie={ck}"} if cookie else {}
        return c.get(url, headers=h, timeout=60.0).text

    casos = {
        "SEM edicao (01/08, dl)": corpo(f"{INLABS_BASE}/index.php?p=2026-08-01&dl=2026-08-01-DO1.zip"),
        "listagem 01/08 (sem dl)": corpo(f"{INLABS_BASE}/index.php?p=2026-08-01"),
        "listagem 31/07 (sem dl)": corpo(f"{INLABS_BASE}/index.php?p=2026-07-31"),
        "SEM cookie (simula deslogado)": corpo(f"{INLABS_BASE}/index.php?p=2026-07-31&dl=2026-07-31-DO1.zip", cookie=False),
    }
    print(f"{'marcador':22} " + " ".join(f"{k[:14]:>15}" for k in casos))
    for m in MARCAS:
        linha = " ".join(f"{str(m.lower() in v.lower()):>15}" for v in casos.values())
        print(f"{m:22} {linha}")
    print()
    for k, v in casos.items():
        print(f"{k}: {len(v)} chars")
