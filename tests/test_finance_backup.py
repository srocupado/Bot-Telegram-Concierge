"""Backup e restore do gerenciador-financeiro pelo bot.

Pedido do dono (08/09/2026): "backup em paralelo com o github e restaurar via
comando no bot".

Os três modos de falha que estes testes existem pra travar:

1. **Restore que mistura duas fotos.** O Firestore faz merge PROFUNDO de mapas
   em `set(merge=True)`. Restaurar com merge deixaria de pé uma seção que
   existe hoje e não existe no backup — você pediria a foto de ontem e
   receberia um híbrido. Tem que ser `update`, que troca `state` inteiro.
2. **Restaurar o arquivo errado em silêncio.** O "Importar JSON" do app faz
   `setState(JSON.parse(f))` sem validar: dar o envelope do backup zera a
   tela e diz "importado com sucesso". `extract_state` tem que descascar o
   envelope e RECUSAR o que não reconhece.
3. **Backup que parou e ninguém soube.** Falha tem que virar aviso.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pytest

from bot.services import finance_backup as fb
from bot.services.finance_backup import (
    BackupError,
    extract_state,
    list_backups,
    purge_old,
    resolve_backup,
    resumo_state,
    write_backup,
)

UID = "abc123uid"


@pytest.fixture(autouse=True)
def _dir_temporario(tmp_path, monkeypatch):
    """Cada teste com sua pasta — senão um vaza backup pro outro."""
    monkeypatch.setattr(fb.settings, "finance_backup_dir", str(tmp_path / "fin"))
    monkeypatch.setattr(fb.settings, "finance_backup_retention_days", 30)
    return tmp_path


def _envelope(state: dict, uid: str = UID) -> dict:
    return {
        "exportedAt": "2026-09-08T04:00:00+00:00",
        "project": "gerenciador-financeiro-1d910",
        "count": 1,
        "users": {uid: {"state": state, "updatedAt": "2026-09-08T03:59:00Z"}},
    }


_STATE = {
    "bankTransactions": [{"id": "b1"}, {"id": "b2"}],
    "cardEntries": [{"id": "c1"}],
    "treasuryHoldings": [],
    "investments": {"assets": [{"id": "a1"}, {"id": "a2"}, {"id": "a3"}]},
    "settings": {"cardClosingDay": 5},
}


# ───────────────── extract_state: as três formas e as recusas ─────────────────

def test_descasca_envelope_do_backup() -> None:
    assert extract_state(_envelope(_STATE), UID) == _STATE


def test_aceita_state_cru_do_exportar_json_do_app() -> None:
    """O botão "Exportar JSON" da sidebar grava o state sem envelope."""
    assert extract_state(_STATE, UID) == _STATE


def test_aceita_doc_de_um_usuario_so() -> None:
    assert extract_state({"state": _STATE}, UID) == _STATE


def test_uid_diferente_com_um_usuario_so_ainda_restaura() -> None:
    """Backup do GitHub pode ter sido gerado antes de trocar de conta. Com UM
    usuário só não há ambiguidade — restaura e loga a divergência."""
    assert extract_state(_envelope(_STATE, uid="outro"), UID) == _STATE


def test_varios_usuarios_sem_o_meu_uid_recusa() -> None:
    """Aqui HÁ ambiguidade: escolher por conta própria sobrescreveria o
    financeiro com o de outra pessoa."""
    env = {"users": {"x": {"state": _STATE}, "y": {"state": _STATE}}}
    with pytest.raises(BackupError, match="2 usuários"):
        extract_state(env, UID)


@pytest.mark.parametrize("lixo", [
    {"foo": "bar"},                  # JSON qualquer
    {"users": {}},                   # envelope vazio
    [1, 2, 3],                       # nem objeto é
    {"state": "não é dict"},
])
def test_recusa_o_que_nao_reconhece(lixo) -> None:
    """Na dúvida NÃO restaura: o custo de recusar é reenviar o arquivo; o de
    aceitar é sobrescrever o financeiro com lixo."""
    with pytest.raises(BackupError):
        extract_state(lixo, UID)


# ───────────────────────── arquivos: escrita, lista, poda ─────────────────────

def test_escrita_atomica_nao_deixa_tmp() -> None:
    p = write_backup(_envelope(_STATE), hoje=date(2026, 9, 8))
    assert p.name == "financeiro-2026-09-08.json"
    assert json.loads(p.read_text())["count"] == 1
    assert list(p.parent.glob("*.tmp")) == [], "sobrou .tmp (escrita não-atômica)"


def test_lista_do_mais_novo_pro_mais_velho() -> None:
    for d in (date(2026, 9, 1), date(2026, 9, 8), date(2026, 9, 5)):
        write_backup(_envelope(_STATE), hoje=d)
    assert [b.nome for b in list_backups()] == [
        "financeiro-2026-09-08.json",
        "financeiro-2026-09-05.json",
        "financeiro-2026-09-01.json",
    ]


def test_resolve_rejeita_travessia_de_diretorio() -> None:
    write_backup(_envelope(_STATE), hoje=date(2026, 9, 8))
    assert resolve_backup("financeiro-2026-09-08.json") is not None
    for veneno in ("../../etc/passwd", "/etc/passwd", "financeiro-2026-09-08.json/../x",
                   "qualquer.json", ""):
        assert resolve_backup(veneno) is None, f"aceitou {veneno!r}"


def test_poda_respeita_retencao_e_nao_poupa_pre_restore() -> None:
    hoje = date(2026, 9, 8)
    write_backup(_envelope(_STATE), hoje=hoje)                       # 0 dias
    write_backup(_envelope(_STATE), hoje=date(2026, 8, 20))          # 19 dias
    write_backup(_envelope(_STATE), hoje=date(2026, 7, 1))           # 69 dias
    write_backup(_envelope(_STATE), hoje=date(2026, 7, 2),
                 sufixo="-pre-restore-101010")                       # 68 dias
    assert purge_old(hoje=hoje) == 2
    assert [b.nome for b in list_backups()] == [
        "financeiro-2026-09-08.json",
        "financeiro-2026-08-20.json",
    ]


def test_retencao_zero_nao_apaga_nada() -> None:
    """0 = desligado. Um `<= 0` mal escrito apagaria o backup de hoje."""
    write_backup(_envelope(_STATE), hoje=date(2026, 1, 1))
    assert purge_old(0, hoje=date(2026, 9, 8)) == 0
    assert len(list_backups()) == 1


def test_upload_entra_na_lista_e_e_restauravel() -> None:
    """JSON mandado no chat vira arquivo local com o mesmo padrão de nome —
    um caminho de código só pro restore."""
    p = write_backup(_envelope(_STATE), hoje=date(2026, 9, 8), sufixo="-upload-143000")
    assert p.name == "financeiro-2026-09-08-upload-143000.json"
    assert resolve_backup(p.name) is not None


# ─────────────────────────── serialização de Timestamp ───────────────────────

def test_serializa_datetime_aninhado() -> None:
    """Firestore devolve DatetimeWithNanoseconds; json.dumps estoura nele e o
    backup do dia inteiro se perderia."""
    class FakeTs:
        def isoformat(self):
            return "2026-09-08T04:00:00+00:00"

    out = fb._serialize({
        "updatedAt": FakeTs(),
        "state": {"bankTransactions": [{"date": datetime(2026, 9, 8, tzinfo=timezone.utc)}]},
    })
    assert out["updatedAt"] == "2026-09-08T04:00:00+00:00"
    assert out["state"]["bankTransactions"][0]["date"].startswith("2026-09-08")
    json.dumps(out)  # tem que serializar sem estourar


# ──────────────── restore: overwrite limpo, não merge profundo ────────────────

class _FakeRef:
    def __init__(self, existe: bool = True):
        self.existe = existe
        self.chamadas: list[tuple[str, dict]] = []

    def get(self):
        return type("S", (), {"exists": self.existe})()

    def update(self, payload):
        self.chamadas.append(("update", payload))

    def set(self, payload, **kw):
        self.chamadas.append(("set", {**payload, **kw}))


class _FakeDb:
    def __init__(self, ref):
        self._ref = ref

    def collection(self, _name):
        return self

    def document(self, _uid):
        return self._ref


def test_restore_usa_update_e_nao_set_merge(monkeypatch) -> None:
    """O ponto mais importante do arquivo. Com `set(merge=True)` o Firestore
    faz merge PROFUNDO: restaurar um state sem `investments` deixaria a
    carteira atual de pé — foto de ontem misturada com hoje."""
    import sys
    import types
    fake_fs = types.SimpleNamespace(SERVER_TIMESTAMP="::ts::")
    monkeypatch.setitem(sys.modules, "firebase_admin",
                        types.SimpleNamespace(firestore=fake_fs))
    monkeypatch.setitem(sys.modules, "firebase_admin.firestore", fake_fs)

    ref = _FakeRef(existe=True)
    fb._restore_blocking(_FakeDb(ref), UID, _STATE)

    assert len(ref.chamadas) == 1
    op, payload = ref.chamadas[0]
    assert op == "update", f"usou {op!r} — merge profundo mistura as duas fotos"
    assert payload["state"] == _STATE
    assert payload["updatedAt"] == "::ts::"


def test_restore_cria_o_doc_quando_nao_existe(monkeypatch) -> None:
    """`update` estoura em doc inexistente — conta zerada não pode travar o
    restore."""
    import sys
    import types
    fake_fs = types.SimpleNamespace(SERVER_TIMESTAMP="::ts::")
    monkeypatch.setitem(sys.modules, "firebase_admin",
                        types.SimpleNamespace(firestore=fake_fs))
    monkeypatch.setitem(sys.modules, "firebase_admin.firestore", fake_fs)

    ref = _FakeRef(existe=False)
    fb._restore_blocking(_FakeDb(ref), UID, _STATE)
    assert ref.chamadas[0][0] == "set"


# ─────────────────────────────── prévia ao usuário ───────────────────────────

def test_resumo_conta_as_secoes() -> None:
    """A confirmação não pode ser às cegas: o dono precisa ver o tamanho do
    que vai escrever antes de apertar o botão."""
    r = resumo_state(_STATE)
    assert "2 banco" in r and "1 cartão" in r and "3 ativos" in r


def test_resumo_de_state_vazio_nao_mente() -> None:
    assert resumo_state({}) == "nenhuma seção reconhecida"


# ─────────────────────────── janelas mutuamente exclusivas ───────────────────

def test_as_duas_janelas_de_json_nao_ficam_abertas_juntas() -> None:
    """Service account e restore capturam o MESMO tipo de arquivo (.json). Com
    as duas janelas abertas, a ordem dos routers decidiria em silêncio se o
    arquivo vira credencial ou SOBRESCREVE o financeiro."""
    from bot.handlers import finance_backup as h_backup
    from bot.handlers import financeiro as h_setup
    import inspect

    setup_src = inspect.getsource(h_setup.cmd_setup)
    assert "awaiting_finance_restore_until = None" in setup_src

    restore_src = inspect.getsource(h_backup.cmd_restaurar)
    assert "awaiting_firebase_json_until = None" in restore_src


# ───────────── scheduler: janela, dedup e o aviso quando quebra ──────────────

class _Bot:
    def __init__(self):
        self.enviadas: list[str] = []

    async def send_message(self, chat_id, text, **kw):
        self.enviadas.append(text)


def _relogio(hora: int):
    class _DT(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 8, hora, 10, tzinfo=tz)
    return _DT


async def _com_dono(sm):
    from bot.db.models import Base, User
    async with sm.kw["bind"].begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with sm() as s:
        s.add(User(id=77, chat_id=77, is_authorized=True, firebase_uid=UID))
        await s.commit()


def _sessionmaker():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import StaticPool
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool, connect_args={"check_same_thread": False},
    )
    return async_sessionmaker(engine, expire_on_commit=False)


def _rodar(monkeypatch, hora: int, *, quebra: bool = False, vezes: int = 1,
           ligado: bool = True):
    """Roda run_finance_backup `vezes` no mesmo dia. Devolve (bot, chamadas)."""
    import asyncio as aio
    from bot.services import scheduler

    monkeypatch.setattr(scheduler, "datetime", _relogio(hora))
    monkeypatch.setattr(scheduler.settings, "finance_backup_enabled", ligado)
    monkeypatch.setattr(scheduler.settings, "finance_backup_hour", 4)
    monkeypatch.setattr(scheduler.settings, "owner_telegram_id", None)

    chamadas = {"n": 0}

    async def _fake_run_backup(session, *, hoje=None):
        chamadas["n"] += 1
        if quebra:
            raise RuntimeError("Firestore fora")
        return write_backup(_envelope(_STATE), hoje=hoje), {"count": 1}

    monkeypatch.setattr(fb, "run_backup", _fake_run_backup)

    sm = _sessionmaker()
    bot = _Bot()

    async def _main():
        await _com_dono(sm)
        for _ in range(vezes):
            await scheduler.run_finance_backup(sm, bot)

    aio.run(_main())
    return bot, chamadas


def test_fora_da_janela_nao_roda(monkeypatch) -> None:
    _bot, chamadas = _rodar(monkeypatch, hora=9)
    assert chamadas["n"] == 0


def test_desligado_nao_roda(monkeypatch) -> None:
    _bot, chamadas = _rodar(monkeypatch, hora=4, ligado=False)
    assert chamadas["n"] == 0


def test_roda_na_hora_alvo(monkeypatch) -> None:
    bot, chamadas = _rodar(monkeypatch, hora=4)
    assert chamadas["n"] == 1
    assert bot.enviadas == [], "backup OK não deve mandar mensagem (só falha avisa)"


def test_catchup_pega_o_dia_quando_o_bot_estava_fora_as_4h(monkeypatch) -> None:
    """Janela larga (4h→8h): deploy ou queda na hora cheia não pode custar o
    backup DO DIA — foi o mesmo motivo do resumo de fechamento da fatura."""
    _bot, chamadas = _rodar(monkeypatch, hora=7)
    assert chamadas["n"] == 1


def test_nao_gera_dois_no_mesmo_dia(monkeypatch) -> None:
    """O tick roda de 60 em 60s dentro de uma janela de 4h: sem dedup seriam
    ~240 leituras do Firestore por dia."""
    _bot, chamadas = _rodar(monkeypatch, hora=4, vezes=5)
    assert chamadas["n"] == 1


def test_falha_avisa_uma_vez_e_continua_tentando(monkeypatch) -> None:
    """Backup que parou e ninguém soube é indistinguível de backup em dia até
    a hora de restaurar. Mas o aviso é 1x/dia, não a cada tick."""
    bot, chamadas = _rodar(monkeypatch, hora=4, quebra=True, vezes=4)
    assert chamadas["n"] == 4, "desistiu de re-tentar depois da 1ª falha"
    assert len(bot.enviadas) == 1, f"avisou {len(bot.enviadas)}x (esperado 1)"
    aviso = bot.enviadas[0]
    assert "Backup do financeiro falhou" in aviso
    assert "nightly-backup" in aviso, "não disse que o do GitHub é independente"
    assert "/financeiro_backup" in aviso, "não disse como tentar na mão"


# ─────────────────────────────── help (regra do projeto) ─────────────────────

@pytest.mark.parametrize("frase", [
    "como faço backup do financeiro?",
    "quero restaurar meus dados",
    "perdi meus lançamentos, tem backup?",
    "como recuperar o financeiro?",
])
def test_help_roteia_backup_e_restore(frase: str) -> None:
    from bot.handlers.start import HELP_TEXT, find_help_sections

    assert "/financeiro_backup" in HELP_TEXT
    assert "/financeiro_restaurar" in HELP_TEXT
    secoes = find_help_sections(frase)
    assert any("financeiro" in s.lower() for s in secoes), f"{frase!r} não achou"
