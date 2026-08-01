"""Inlabs nunca devolve 404: HTTP 200 com HTML em TODOS os casos ruins.

Medido contra o Inlabs real em 01/08/2026 (sábado sem edição), de dentro do
container:

    2026-07-31 DO1 : HTTP 200 | 11.756.017 bytes | ZIP  (dia com edição)
    2026-08-01 DO1 : HTTP 200 |     37.583 bytes | HTML "Imprensa Nacional - INLABS"
    sem cookie     : HTTP 200 |      6.032 bytes | HTML com type="password"

Confundir os dois HTMLs custa caro nas DUAS direções:
- login lido como "não publicado" → dia recebe baixa e a MP some em silêncio;
- listagem lida como falha → alarme "Inlabs indisponível" todo fim de semana
  sem edição extra, mais nota na fila re-tentada por 14 dias.

O segundo foi o que aconteceu: `/mp_dou_agora` num sábado respondeu
"seção(ões) DO1E, DO1 indisponíveis no Inlabs" quando a verdade era
"não há edição publicada nessa data".
"""
from __future__ import annotations

from bot.services import dou_monitor as dm

# Trechos reais das respostas observadas (recortados).
LOGIN = (
    '<!DOCTYPE html> <html> <head> <title>Imprensa Nacional - INLABS</title> '
    '<link rel="stylesheet" href="css/bootstrap.min.css"> </head><body>'
    '<form action="logar.php" method="post">'
    '<input type="email" name="email"><input type="password" name="password">'
    '</form></body></html>'
)
LISTAGEM = (
    '<!DOCTYPE html> <html> <head> <title>Imprensa Nacional - INLABS</title> '
    '<link rel="stylesheet" href="css/bootstrap.min.css"> </head><body>'
    '<p>Olá vinicius.const@gmail.com</p><a href="sair.php">Sair</a>'
    '<table><tr><th>Nome</th><th>Tamanho</th><th>Modificado</th></tr>'
    '<tr><td>2026-07-31</td><td>143.01 MB</td></tr></table></body></html>'
)
MANUTENCAO = (
    "<html><body>Sistema em manutenção. Tente mais tarde.</body></html>"
)


def test_login_e_reconhecido_como_sessao_recusada() -> None:
    assert dm._E_LOGIN_RE.search(LOGIN)


def test_listagem_nao_e_confundida_com_login() -> None:
    """O ponto do conserto: listagem não pode cair na regra de login."""
    assert not dm._E_LOGIN_RE.search(LISTAGEM)
    assert dm._e_listagem(LISTAGEM)


def test_login_nao_passa_por_listagem_nem_fora_de_ordem() -> None:
    """As duas páginas têm o MESMO título "Imprensa Nacional - INLABS"
    (medido). Se a listagem fosse reconhecida pelo título, a classificação
    ficaria de pé só pela ORDEM das checagens — e bastaria o Inlabs mudar o
    formulário de login pra sessão recusada virar "não publicado", dando baixa
    no dia e perdendo a MP em silêncio. Os marcadores do navegador de arquivos
    (Sair/Tamanho/Modificado, ausentes no login) tornam isso impossível.
    """
    assert "Imprensa Nacional - INLABS" in LOGIN
    assert not dm._e_listagem(LOGIN)


def test_marcadores_da_listagem_sao_os_medidos() -> None:
    """Medido em 01/08/2026: listagem (37.549 chars) tem os três; login
    (6.032) não tem nenhum."""
    for marca in dm._MARCAS_LISTAGEM:
        assert marca in LISTAGEM.lower(), marca
        assert marca not in LOGIN.lower(), marca


def test_manutencao_continua_tendo_precedencia() -> None:
    """Manutenção é checada ANTES das outras duas — é pane, não ausência."""
    assert dm._MAINT_RE.search(MANUTENCAO)
    assert not dm._e_listagem(MANUTENCAO)


def test_corpo_desconhecido_nao_casa_com_nada() -> None:
    """Padrão seguro: o que não for reconhecido vira FALHA (dia pendente),
    nunca 'não publicado'."""
    estranho = "<html><body>502 Bad Gateway</body></html>"
    assert not dm._E_LOGIN_RE.search(estranho)
    assert not dm._e_listagem(estranho)
    assert not dm._MAINT_RE.search(estranho)


def test_sabado_sem_edicao_fica_provisorio_e_nao_alarma() -> None:
    """Sábado de manhã ainda pode receber edição extra: sem baixa, sem alarme.

    É a régua que evita trocar um erro (alarme falso) por outro (dar o dia
    por vazio cedo demais e perder a extra que sai à noite).
    """
    from datetime import date, datetime
    sabado = date(2026, 8, 1)
    manha = datetime(2026, 8, 1, 8, tzinfo=dm.BRT)
    assert not dm._dia_encerrado(sabado, manha)
    # Domingo depois das 6h: aí sim "não publicado" é definitivo.
    assert dm._dia_encerrado(sabado, datetime(2026, 8, 2, 7, tzinfo=dm.BRT))


def test_erro_de_login_nao_culpa_a_credencial() -> None:
    """Medido: o mesmo par e-mail/senha logou e, minutos depois, não. Mandar
    o dono conferir o .env é mandá-lo atrás de problema que não é dele."""
    import inspect
    fonte = inspect.getsource(dm._fetch_mps_sync)
    assert "não abriu sessão agora" in fonte
    assert "verifique e-mail/senha" not in fonte


# ───────────────────── comportamento de ponta a ponta ─────────────────────

class _Resp:
    def __init__(self, status: int, content: bytes):
        self.status_code = status
        self.content = content
        self.text = content.decode("utf-8", "replace")
        self.request = None

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"status inesperado {self.status_code}")


class _Inlabs:
    """Inlabs falso servindo o MESMO corpo observado em produção."""

    def __init__(self, corpo: str):
        self.corpo = corpo.encode()
        self.cookies = {"inlabs_session_cookie": "c"}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, *a, **kw):
        return _Resp(200, b"ok")

    def get(self, url, **kw):
        return _Resp(200, self.corpo)


def _rodar(monkeypatch, corpo: str, alvo):
    from types import SimpleNamespace
    monkeypatch.setattr(dm.httpx, "Client", lambda **kw: _Inlabs(corpo))
    monkeypatch.setattr(dm.settings, "inlabs_email", "e@x")
    monkeypatch.setattr(dm.settings, "inlabs_password",
                        SimpleNamespace(get_secret_value=lambda: "s"))
    return dm._fetch_mps_sync(alvo)


def test_listagem_vira_nao_publicado_e_nao_falha(monkeypatch) -> None:
    """O caso de hoje: sábado sem edição não pode virar 'Inlabs indisponível'."""
    from datetime import datetime
    hoje = datetime.now(dm.BRT).date()
    out = _rodar(monkeypatch, LISTAGEM, hoje)
    assert out.incompleto is False, "listagem virou FALHA — alarme falso volta"
    assert out.provisorio is True, "dia aberto: sem baixa, re-checa mais tarde"
    assert set(out.secoes_404) == set(dm.DOU_SECTIONS)


def test_login_continua_sendo_falha(monkeypatch) -> None:
    """Sessão recusada NÃO pode virar 'não houve MP' — perderia a MP calado.

    Com as duas seções falhando e nenhum resultado, o fetch levanta DouError:
    o caller diz 'não consegui checar', que é a verdade.
    """
    import pytest
    from datetime import datetime
    with pytest.raises(dm.DouError, match="não consegui baixar o DOU"):
        _rodar(monkeypatch, LOGIN, datetime.now(dm.BRT).date())
