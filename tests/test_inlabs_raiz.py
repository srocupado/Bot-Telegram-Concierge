"""Pasta inexistente no Inlabs ≠ sessão recusada (bug severo, 10/08/2026).

Medido no Pi com a sessão do bot: pra `?p=<dia sem pasta>` o Inlabs serve a
listagem RAIZ (HTTP 200, com marcas de listagem). Sem distinguir raiz de
pasta-do-dia, o bug cortava dos dois lados:
- fim de semana sem edição preso na fila como "recusou a sessão" (08-09/08);
- raiz servida por blip no lugar da pasta de um dia QUE EXISTE viraria
  "sem edição" com baixa — MP perdida em silêncio (o lado pior).
"""
from __future__ import annotations

import io
import zipfile
from datetime import date, timedelta
from types import SimpleNamespace

import httpx
import pytest
import respx

from bot.services import dou_monitor
from bot.services.dou_monitor import DouError, _fetch_mps_sync, _invalidar_sessao


RAIZ_SEM_O_DIA = (
    "<html>Sair Tamanho Modificado "
    '<a href="index.php?p=2026-08-10">2026-08-10</a> '
    '<a href="index.php?p=2026-08-07">2026-08-07</a> '
    '<a href="index.php?p=2026-07-31">2026-07-31</a></html>'
)


def _raiz_com(dia_iso: str) -> str:
    return RAIZ_SEM_O_DIA.replace(
        "</html>", f'<a href="index.php?p={dia_iso}">{dia_iso}</a></html>'
    )


def _zip_sem_mp() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("ato.xml", "<xml>Portaria qualquer, sem a palavra magica</xml>")
    return buf.getvalue()


def _preparar(monkeypatch, corpo_listagem, *, zip_do_dia: bytes | None = None):
    from pydantic import SecretStr

    monkeypatch.setattr(dou_monitor.settings, "inlabs_email", "x@y.z")
    monkeypatch.setattr(dou_monitor.settings, "inlabs_password", SecretStr("s"))
    monkeypatch.setattr(dou_monitor.time, "sleep", lambda _s: None)
    _invalidar_sessao()

    def _rota(request):
        url = str(request.url)
        if "logar.php" in url:
            return httpx.Response(
                200, text="ok",
                headers={"set-cookie": "inlabs_session_cookie=abc; Path=/"},
            )
        if "dl=" in url:
            return httpx.Response(200, content=zip_do_dia or b"")
        return httpx.Response(200, text=corpo_listagem)

    respx.route(host="inlabs.in.gov.br").side_effect = _rota


def test_dia_fechado_sem_pasta_e_sem_edicao_conclusivo(monkeypatch) -> None:
    """08/08 e 09/08 (sem pasta, dias fechados): raiz viva cobrindo datas mais
    antigas = evidência positiva → sem_edicao, completo, SEM provisorio — o
    dia recebe baixa em vez de 'recusou a sessão' eterno."""
    with respx.mock:
        _preparar(monkeypatch, RAIZ_SEM_O_DIA)
        out = _fetch_mps_sync(date(2026, 8, 9))
    assert list(out) == []
    assert out.sem_edicao is True
    assert out.provisorio is False and out.incompleto is False


def test_dia_aberto_sem_pasta_fica_provisorio(monkeypatch) -> None:
    """Hoje sem pasta ainda pode ganhar edição — provisorio, sem baixa."""
    hoje = dou_monitor.datetime.now(dou_monitor.BRT).date()
    raiz = (
        "<html>Sair Tamanho Modificado "
        f'<a href="index.php?p={(hoje - timedelta(days=1)).isoformat()}">ontem</a>'
        f"{(hoje - timedelta(days=1)).isoformat()}</html>"
    )
    with respx.mock:
        _preparar(monkeypatch, raiz)
        out = _fetch_mps_sync(hoje)
    assert out.sem_edicao is True and out.provisorio is True


def test_raiz_no_lugar_de_pasta_existente_nao_da_baixa(monkeypatch) -> None:
    """O lado PIOR do bug: a pasta do dia existe na raiz, mas o Inlabs serve a
    raiz no lugar dela. Concluir 'sem edição' daria baixa num dia com DOU —
    MP perdida em silêncio. Tem que falhar ALTO (pendência), com mensagem
    dizendo o que houve (não 'recusou a sessão')."""
    alvo = date(2026, 8, 7)
    with respx.mock:
        _preparar(monkeypatch, _raiz_com("2026-08-07"))
        with pytest.raises(DouError) as exc:
            _fetch_mps_sync(alvo)
    assert "raiz no lugar da pasta" in str(exc.value)


def test_raiz_sem_cobertura_do_periodo_falha_explicito(monkeypatch) -> None:
    """Raiz só com datas POSTERIORES ao alvo: ausência da pasta não prova nada
    (range/paginação) — falha explícita, nunca 'sem edição'."""
    alvo = date(2026, 6, 1)   # bem antes de tudo que a raiz mostra
    with respx.mock:
        _preparar(monkeypatch, RAIZ_SEM_O_DIA)
        with pytest.raises(DouError):
            _fetch_mps_sync(alvo)


def test_listagem_da_pasta_do_dia_segue_normal(monkeypatch) -> None:
    """Listagem real da pasta (só a própria data nos nomes de arquivo) não é
    confundida com a raiz: baixa o zip e conclui 'houve DOU, 0 MP'."""
    listagem = (
        "<html>Sair Tamanho Modificado "
        '<a href="index.php?p=2026-08-05&dl=2026-08-05-DO1.zip">'
        "2026-08-05-DO1.zip</a></html>"
    )
    with respx.mock:
        _preparar(monkeypatch, listagem, zip_do_dia=_zip_sem_mp())
        out = _fetch_mps_sync(date(2026, 8, 5))
    assert list(out) == []
    assert out.sem_edicao is False       # houve DOU (DO1 presente), só sem MP
    assert out.incompleto is False


def test_todos_os_cookies_do_login_sao_reenviados(monkeypatch) -> None:
    """Comportamento MEDIDO no Pi (10/08/2026): o login seta PHPSESSID + TS*
    além do inlabs_session_cookie, e pra pasta INEXISTENTE o Inlabs decide
    "raiz ou login?" pela PHPSESSID. Só reenviar o cookie de sessão fazia dia
    sem pasta cair na tela de login (pasta existente respondia normal — por
    isso o dia corrente passava e o fim de semana morria). O mock reproduz
    exatamente isso: sem PHPSESSID no Cookie → tela de login."""
    from pydantic import SecretStr

    monkeypatch.setattr(dou_monitor.settings, "inlabs_email", "x@y.z")
    monkeypatch.setattr(dou_monitor.settings, "inlabs_password", SecretStr("s"))
    monkeypatch.setattr(dou_monitor.time, "sleep", lambda _s: None)
    _invalidar_sessao()
    login_page = '<html><form action="logar.php"><input type="password"></form></html>'

    def _rota(request):
        url = str(request.url)
        if "logar.php" in url:
            return httpx.Response(200, text="ok", headers=[
                ("set-cookie", "inlabs_session_cookie=abc; Path=/"),
                ("set-cookie", "PHPSESSID=xyz; Path=/"),
                ("set-cookie", "TS016f630c=waf1; Path=/"),
            ])
        if "PHPSESSID=xyz" not in request.headers.get("cookie", ""):
            return httpx.Response(200, text=login_page)
        return httpx.Response(200, text=RAIZ_SEM_O_DIA)

    with respx.mock:
        respx.route(host="inlabs.in.gov.br").side_effect = _rota
        out = _fetch_mps_sync(date(2026, 8, 9))
    assert out.sem_edicao is True and out.provisorio is False


def test_login_aquecido_como_navegador(monkeypatch) -> None:
    """WAF F5 de 11/08/2026: POST 'frio' de login levava 5xx com página de
    manutenção FALSA, enquanto o fluxo de navegador (GET da tela de login
    antes + POST com Referer/Origin) logava no mesmo minuto. O bot agora
    imita o navegador — e a ordem/headers são verificados aqui."""
    from pydantic import SecretStr

    monkeypatch.setattr(dou_monitor.settings, "inlabs_email", "x@y.z")
    monkeypatch.setattr(dou_monitor.settings, "inlabs_password", SecretStr("s"))
    monkeypatch.setattr(dou_monitor.time, "sleep", lambda _s: None)
    _invalidar_sessao()
    seq: list[tuple[str, str, str | None, str | None]] = []

    def _rota(request):
        url = str(request.url)
        metodo = request.method
        if isinstance(metodo, bytes):
            metodo = metodo.decode()
        seq.append((metodo, request.url.path,
                    request.headers.get("referer"),
                    request.headers.get("origin")))
        if "logar.php" in url:
            return httpx.Response(200, text="ok", headers={
                "set-cookie": "inlabs_session_cookie=abc; Path=/"})
        if "acessar.php" in url:
            return httpx.Response(200, text="<form action='logar.php'></form>")
        return httpx.Response(200, text=RAIZ_SEM_O_DIA)

    with respx.mock:
        respx.route(host="inlabs.in.gov.br").side_effect = _rota
        out = _fetch_mps_sync(date(2026, 8, 9))
    assert out.sem_edicao is True                       # fluxo inteiro passou
    assert (seq[0][0], seq[0][1]) == ("GET", "/acessar.php"), "aquece antes"
    post = next(s for s in seq if s[1] == "/logar.php")
    assert post[2] and post[2].endswith("/acessar.php"), "POST leva Referer"
    assert post[3] == "https://inlabs.in.gov.br", "POST leva Origin"


def test_tela_de_login_continua_sendo_recusa(monkeypatch) -> None:
    """A recusa REAL de sessão continua falhando como antes."""
    login = '<html><form action="logar.php"><input type="password"></form></html>'
    with respx.mock:
        _preparar(monkeypatch, login)
        with pytest.raises(DouError) as exc:
            _fetch_mps_sync(date(2026, 8, 9))
    assert "recusou a sessão" in str(exc.value)
