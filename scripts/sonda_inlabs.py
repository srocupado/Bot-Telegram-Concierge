"""Sonda: o que o Inlabs responde num dia COM edição vs num dia SEM edição.

Usa a mesma pilha do bot (httpx + headers + fluxo de login de
_fetch_mps_sync), pra não medir num cliente diferente do que roda em produção.
"""
import httpx

from bot.config import settings
from bot.services.dou_monitor import _HEADERS, INLABS_BASE

email = settings.inlabs_email
senha = settings.inlabs_password.get_secret_value() if settings.inlabs_password else None
print("credenciais:", bool(email), bool(senha))

with httpx.Client(headers=_HEADERS, follow_redirects=True) as c:
    r = c.post(
        f"{INLABS_BASE}/logar.php",
        data={"email": email, "password": senha},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=20.0,
    )
    cookie = c.cookies.get("inlabs_session_cookie")
    print(f"login: HTTP {r.status_code} | cookie: {'ok' if cookie else 'AUSENTE'}")

    for dia, rotulo in (("2026-07-31", "COM edição (controle)"),
                        ("2026-08-01", "SEM edição (o caso de hoje)")):
        print(f"\n=== {dia} — {rotulo} ===")
        for sec in ("DO1E", "DO1"):
            url = f"{INLABS_BASE}/index.php?p={dia}&dl={dia}-{sec}.zip"
            try:
                rr = c.get(url, headers={"Cookie": f"inlabs_session_cookie={cookie}"},
                           timeout=60.0)
            except Exception as exc:
                print(f"{sec}: EXCEÇÃO {type(exc).__name__}: {exc}")
                continue
            corpo = rr.content
            eh_zip = corpo[:2] == b"PK"
            print(f"{sec}: HTTP {rr.status_code} | {len(corpo)} bytes | "
                  f"content-type={rr.headers.get('content-type')} | ZIP={eh_zip}")
            if not eh_zip:
                texto = corpo.decode("utf-8", "replace")
                texto = " ".join(texto.split())
                print(f"    corpo[:300]: {texto[:300]}")
