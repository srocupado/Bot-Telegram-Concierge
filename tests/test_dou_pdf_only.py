"""Edição extra publicada SÓ em PDF (sábado/feriado): o Inlabs não gera o
XML/zip, só `do1_extra_*.pdf`. Medido contra o Inlabs real em 01/08/2026 — a
MP 1.382 saiu exatamente assim, e o bot (que só lia zip) a perdia em silêncio.

Garante duas coisas:
1. a listagem leva ao PDF e a MP do CABEÇALHO é entregue;
2. as MPs CITADAS no corpo ("altera a MP nº X") NÃO viram MP fantasma — a
   âncora é caixa alta + início de linha (medido: extra_C tem a 1.382 no
   cabeçalho e cita 1.373/1.355/1.350; extra_D só cita → 0 MP).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from bot.services import dou_monitor
from bot.services.dou_monitor import BRT


# Recorte fiel do texto extraído do extra_C (via pypdf): cabeçalho em CAIXA
# ALTA no início da linha + citações em caixa de título no meio das frases.
TEXTO_EXTRA_C = (
    "Atos do Poder Executivo\n"
    "MEDIDA PROVISÓRIA Nº 1.382, DE 1º DE AGOSTO DE 2026\n"
    "Altera a Lei nº 14.042, de 19 de agosto de 2020, e a Medida Provisória nº\n"
    "1.373, de 29 de junho de 2026, para autorizar a combinação de recursos.\n"
    "O PRESIDENTE DA REPÚBLICA resolve:\n"
    "Art. 4º A Medida Provisória nº 1.355, de 4 de maio de 2026, passa a vigorar\n"
    "II - a Medida Provisória nº 1.350, de 15 de abril de 2026.\n"
)
# extra_D: só CITA MP (nenhum cabeçalho em caixa alta no início de linha).
TEXTO_EXTRA_D = (
    "PORTARIA Nº 10, DE 1º DE AGOSTO DE 2026\n"
    "Considerando o disposto no § 5º da Medida Provisória nº 1.355, de 4 de maio\n"
    "de 2026, resolve:\n"
    "Art. 1º Fica prorrogado até 31 de dezembro de 2026.\n"
)


class _SemRede:
    """Client cujo GET falha — força o _build_mp_dict a cair no fallback do
    excerpt (ementa do próprio texto), sem tocar a rede."""

    def get(self, *a, **kw):
        raise RuntimeError("sem rede no teste")


# ─────────────────────── parser de texto (o coração) ───────────────────────

def test_parse_texto_pega_cabecalho_e_ignora_citacoes() -> None:
    mps = dou_monitor._parse_dou_text(_SemRede(), TEXTO_EXTRA_C, date(2026, 8, 1))
    assert [mp["numero"] for mp in mps] == ["1382"], "citação virou MP fantasma"
    assert mps[0]["ano"] == 2026, "ano deve sair do título da própria MP"


def test_parse_texto_so_citacao_nao_gera_mp() -> None:
    mps = dou_monitor._parse_dou_text(_SemRede(), TEXTO_EXTRA_D, date(2026, 8, 1))
    assert mps == [], "documento que só cita MP não pode gerar MP"


# ───────────────────── classificação das fontes da pasta ─────────────────────

def test_fontes_separa_zip_pdf_normal_extra() -> None:
    nomes = {
        "2026-07-30-DO1.zip", "2026-07-30-DO1E.zip", "2026-07-30-DO2.zip",
        "2026_07_30_ASSINADO_do1.pdf", "2026_07_30_ASSINADO_do1_extra_A.pdf",
    }
    f = dou_monitor._fontes_secao1(nomes)
    assert f["DO1"]["zip"] == ["2026-07-30-DO1.zip"]
    assert f["DO1E"]["zip"] == ["2026-07-30-DO1E.zip"]
    assert f["DO1"]["pdf"] == ["2026_07_30_ASSINADO_do1.pdf"]
    assert f["DO1E"]["pdf"] == ["2026_07_30_ASSINADO_do1_extra_A.pdf"], (
        "normal não pode capturar o pdf de extra e vice-versa"
    )


# ───────────────────── ponta a ponta: pasta só com PDF ─────────────────────

class _Resp:
    def __init__(self, status: int, content: bytes):
        self.status_code = status
        self.content = content
        self.text = content.decode("utf-8", "replace")
        self.request = None

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"status inesperado {self.status_code}")


def _listing(nomes) -> bytes:
    linhas = "".join(f'<a href="index.php?p=x&dl={n}">{n}</a> ' for n in nomes)
    return (f"<html><body>Ola<a href='sair.php'>Sair</a>"
            f"<table><th>Nome</th><th>Tamanho</th><th>Modificado</th>"
            f"{linhas}</table></body></html>").encode()


class _Inlabs:
    def __init__(self, nomes, bytes_por_nome):
        self.nomes = nomes
        self.bytes_por_nome = bytes_por_nome
        self.cookies = {"inlabs_session_cookie": "c"}
        self.baixadas: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, *a, **kw):
        return _Resp(200, b"ok")

    def get(self, url, **kw):
        if "&dl=" not in url:
            return _Resp(200, _listing(self.nomes))
        nome = url.split("&dl=", 1)[1]
        self.baixadas.append(nome)
        return _Resp(200, self.bytes_por_nome.get(nome, b""))


def _prep(monkeypatch, inlabs) -> None:
    monkeypatch.setattr(dou_monitor.httpx, "Client", lambda **kw: inlabs)
    monkeypatch.setattr(dou_monitor.settings, "inlabs_email", "e@x")
    monkeypatch.setattr(dou_monitor.settings, "inlabs_password",
                        SimpleNamespace(get_secret_value=lambda: "s"))
    # _build_mp_dict não deve bater na rede do Planalto neste teste.
    monkeypatch.setattr(dou_monitor, "_fetch_mp_page", lambda *a, **kw: ("", ""))


def test_extra_so_pdf_entrega_a_mp(monkeypatch) -> None:
    alvo = datetime.now(BRT).date() - timedelta(days=3)   # dia fechado
    pdf = "2026_08_01_ASSINADO_do1_extra_C.pdf"
    inlabs = _Inlabs([pdf], {pdf: b"%PDF-1.4 conteudo"})
    _prep(monkeypatch, inlabs)
    monkeypatch.setattr(dou_monitor, "_extrair_texto_pdf", lambda c: TEXTO_EXTRA_C)

    out = dou_monitor._fetch_mps_sync(alvo)

    assert [mp["numero"] for mp in out] == ["1382"], "MP do PDF não foi entregue"
    assert out[0]["edicao"] == "Extra"
    assert out.incompleto is False, "PDF lido com sucesso não é falha"


def test_pdf_ilegivel_e_falha_nao_vazio(monkeypatch) -> None:
    """PDF que não extrai texto pode ESCONDER uma MP → é falha (pendência),
    nunca 'não houve MP'. O fetch levanta pro caller dizer 'não consegui'."""
    alvo = datetime.now(BRT).date() - timedelta(days=3)
    pdf = "2026_08_01_ASSINADO_do1_extra_C.pdf"
    inlabs = _Inlabs([pdf], {pdf: b"%PDF-quebrado"})
    _prep(monkeypatch, inlabs)
    monkeypatch.setattr(dou_monitor, "_extrair_texto_pdf", lambda c: "")

    with pytest.raises(dou_monitor.DouError, match="não consegui baixar o DOU"):
        dou_monitor._fetch_mps_sync(alvo)


def test_extrair_pdf_lixo_degrada_sem_estourar() -> None:
    """Bytes que não são PDF: os dois motores (PyMuPDF→pypdf) falham em
    sequência e a função devolve "" — nunca levanta. É o que garante que um
    corpo estranho vira FALHA controlada lá em cima, não um crash do fetch."""
    assert dou_monitor._extrair_texto_pdf(b"isto nao e um pdf") == ""
