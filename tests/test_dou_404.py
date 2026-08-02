"""404 do Inlabs: "não publicado" ou "ainda não saiu / instabilidade"?

Antes, todo 404 era lido como seção legitimamente não publicada: o dia
recebia baixa na hora, sem pendência e sem aviso. Com o Inlabs instável (ou
com a DO1E saindo tarde — é onde sai crédito extraordinário), isso perde MP
em silêncio, que é o pior modo de falha possível aqui.

A régua agora é temporal: enquanto o dia pode receber edição, 404 não prova
ausência. Vale pra TODAS as seções de DOU_SECTIONS, não só a extra.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from types import SimpleNamespace

from bot.services import dou_monitor, proactive
from bot.services.dou_monitor import BRT, MPList, _dia_encerrado


def _em(ano, mes, dia, hora) -> datetime:
    return datetime(ano, mes, dia, hora, tzinfo=BRT)


def test_dia_so_encerra_na_manha_seguinte() -> None:
    d = date(2026, 7, 31)
    assert not _dia_encerrado(d, _em(2026, 7, 31, 19)), (
        "19h do próprio dia: edição extra ainda pode sair"
    )
    assert not _dia_encerrado(d, _em(2026, 7, 31, 23)), "ainda é o dia"
    assert not _dia_encerrado(d, _em(2026, 8, 1, 5)), (
        "05h: antes do fechamento, ainda não dá pra afirmar ausência"
    )
    assert _dia_encerrado(d, _em(2026, 8, 1, 6)), "06h do dia seguinte: fechado"
    assert _dia_encerrado(d, _em(2026, 8, 3, 12)), "dias depois: fechado"


def test_briefing_das_7h_ja_fecha_o_dia_anterior() -> None:
    """A régua tem que casar com a cadência das janelas (7/13/19), senão o
    dia anterior ficaria pendente por mais uma rodada inteira à toa."""
    ontem = date(2026, 7, 31)
    assert _dia_encerrado(ontem, _em(2026, 8, 1, 7))


class _FakeSession:
    def __init__(self, *respostas):
        self._respostas = list(respostas)

    async def scalars(self, _stmt):
        return list(self._respostas.pop(0)) if self._respostas else []

    async def commit(self):
        return None


def _colher_com(monkeypatch, resultado, marcadas: list[tuple[str, str]]):
    async def _fetch(_d):
        return resultado

    async def _false(*a, **kw):
        return False

    async def _mark(_session, _uid, kind, key):
        marcadas.append((kind, key))

    async def _none(*a, **kw):
        return None

    monkeypatch.setattr(dou_monitor, "fetch_mps", _fetch)
    monkeypatch.setattr(proactive, "already_notified", _false)
    monkeypatch.setattr(proactive, "mark_notified", _mark)
    monkeypatch.setattr(proactive, "unmark_notified", _none)
    user = SimpleNamespace(id=77, dou_mp_subscribed=True, is_authorized=True,
                            dou_ultimo_dia_ok=None)
    hoje = datetime.now(proactive.BRT).date()
    return asyncio.run(proactive.collect_mp(_FakeSession([], []), user, [hoje])), hoje


def test_404_com_dia_aberto_deixa_pendencia_e_nao_alarma(monkeypatch) -> None:
    """Pendência sim (pra re-checar), aviso não (não houve falha)."""
    provisorio = MPList()
    provisorio.provisorio = True
    provisorio.secoes_404 = ("DO1E",)
    marcadas: list[tuple[str, str]] = []

    facts, hoje = _colher_com(monkeypatch, provisorio, marcadas)

    assert ("mp_pendente", hoje.isoformat()) in marcadas, (
        "dia com 404 recebeu baixa — não será re-checado e a MP some"
    )
    assert [f.kind for f in facts] == [], (
        "404 de dia aberto virou alarme; seria alarme falso todo dia útil "
        "(DO1E só existe quando há edição extra)"
    )


def test_falha_real_continua_alarmando(monkeypatch) -> None:
    """A separação 404 × falha não pode ter silenciado a falha de verdade."""
    incompleto = MPList()
    incompleto.incompleto = True
    incompleto.secoes_falhas = ("DO1",)
    marcadas: list[tuple[str, str]] = []

    facts, hoje = _colher_com(monkeypatch, incompleto, marcadas)

    assert ("mp_pendente", hoje.isoformat()) in marcadas
    assert [f.kind for f in facts] == ["mp_fail"]
    assert "Não consegui checar" in facts[0].text


def test_dia_limpo_recebe_baixa(monkeypatch) -> None:
    """Sem 404 e sem falha: nada de pendência (senão a fila cresce à toa)."""
    marcadas: list[tuple[str, str]] = []

    facts, _ = _colher_com(monkeypatch, MPList(), marcadas)

    assert marcadas == []
    assert facts == []


class _Resp:
    def __init__(self, status: int, content: bytes = b""):
        self.status_code = status
        self.content = content
        self.text = content.decode("utf-8", "replace")
        self.request = None

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"raise_for_status inesperado ({self.status_code})")


def _listing_html(nomes) -> bytes:
    """HTML de listagem com os marcadores medidos (Sair/Tamanho/Modificado) e
    um link de download `&dl=<arquivo>` por arquivo."""
    linhas = "".join(
        f'<a href="index.php?p=x&dl={n}">{n}</a> ' for n in nomes
    )
    return (f"<html><body>Ola<a href='sair.php'>Sair</a>"
            f"<table><th>Nome</th><th>Tamanho</th><th>Modificado</th>"
            f"{linhas}</table></body></html>").encode()


class _FakeClient:
    """Inlabs falso: login OK; a listagem oferece exatamente `nomes`; o
    download por `&dl=` devolve ZIP pra quem está na lista, 404 pro resto."""

    def __init__(self, nomes):
        self.nomes = list(nomes)
        self.cookies = {"inlabs_session_cookie": "c"}
        self.baixadas: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, *a, **kw):
        return _Resp(200, b"ok")

    def get(self, url, **kw):
        if "&dl=" not in url:                       # LISTAGEM do dia
            return _Resp(200, _listing_html(self.nomes))
        nome = url.split("&dl=", 1)[1]
        self.baixadas.append(nome)
        if nome in self.nomes:
            return _Resp(200, b"PK" + b"\0" * 200)  # ZIP "válido" o bastante
        return _Resp(404, b"")


def _fetch_com_listagem(monkeypatch, nomes, alvo: date):
    cliente = _FakeClient(nomes)
    monkeypatch.setattr(dou_monitor.httpx, "Client", lambda **kw: cliente)
    monkeypatch.setattr(dou_monitor.settings, "inlabs_email", "e@x")
    monkeypatch.setattr(
        dou_monitor.settings, "inlabs_password",
        SimpleNamespace(get_secret_value=lambda: "s"),
    )
    # ZIP falso não abre; o que importa aqui é a classificação da listagem,
    # então o parse vira no-op e a seção "200" não polui o resultado.
    monkeypatch.setattr(dou_monitor.zipfile, "ZipFile", _zip_vazio)
    return dou_monitor._fetch_mps_sync(alvo), cliente


class _zip_vazio:
    def __init__(self, *a, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def namelist(self):
        return []


def test_secao_ausente_da_listagem_marca_provisorio(monkeypatch) -> None:
    """Seção da Seção 1 que NÃO aparece na listagem, com o dia ainda aberto, é
    provisória (re-checa) — não falha. A régua é por slot varrido (DO1E/DO1),
    não uma exceção hardcoded; se alguém mexer no par, este teste acusa."""
    hoje = datetime.now(BRT).date()          # dia aberto
    zip_de = {"DO1E": f"{hoje.isoformat()}-DO1E.zip",
              "DO1": f"{hoje.isoformat()}-DO1.zip"}
    for ausente in dou_monitor.DOU_SECTIONS:
        nomes = [v for k, v in zip_de.items() if k != ausente]
        out, _ = _fetch_com_listagem(monkeypatch, nomes, hoje)
        assert out.provisorio is True, f"{ausente} ausente não virou provisório"
        assert out.secoes_404 == (ausente,)
        assert out.incompleto is False, f"{ausente} ausente virou 'falha' (alarme falso)"


def test_dia_encerrado_sem_secao1_e_definitivo(monkeypatch) -> None:
    """Dia fechado e a listagem sem Seção 1: ausência de verdade — sem
    pendência eterna, sem alarme."""
    antigo = datetime.now(BRT).date() - timedelta(days=3)
    out, _ = _fetch_com_listagem(monkeypatch, [], antigo)   # listagem vazia
    assert out.provisorio is False
    assert out.incompleto is False


def test_zip_da_listagem_e_baixado_e_pdf_ignorado_quando_ha_zip(monkeypatch) -> None:
    """Havendo zip do slot, o PDF gêmeo é redundante e NÃO é baixado."""
    hoje = datetime.now(BRT).date()
    nomes = [f"{hoje.isoformat()}-DO1E.zip", f"{hoje.isoformat()}-DO1.zip",
             f"{hoje.strftime('%Y_%m_%d')}_ASSINADO_do1_extra_A.pdf"]
    out, cliente = _fetch_com_listagem(monkeypatch, nomes, hoje)
    assert any(n.endswith("-DO1E.zip") for n in cliente.baixadas)
    assert not any(n.endswith(".pdf") for n in cliente.baixadas), (
        "PDF baixado à toa mesmo com o zip do slot presente"
    )
    assert out.incompleto is False and out.provisorio is False
