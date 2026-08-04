"""Agente proativo (opt-in): avisa o usuário por conta própria, sem ser
perguntado. Gatilhos 100% determinísticos (queries); o LLM entra só como
redator opcional (PROACTIVE_USE_LLM) dos fatos já coletados — nunca decide
o que vigiar nem inventa dados.

Categorias:
- vencimentos: lembretes chegando (não recorrentes) + vencimento da fatura.
- tarefas: tarefas abertas (/tarefas) no briefing matinal e no resumo do fim
  do dia — lembrete até concluir (sem dedup).
- mp: Medidas Provisórias novas no DOU (substitui o digest fixo das 18h).
- nudges: inatividade (treino, lançamentos financeiros, lista de compras).

Janelas: PROACTIVE_HOURS (BRT). Na hora do briefing (PROACTIVE_BRIEFING_HOUR)
consolida e cobre também as MPs do dia anterior (pega edições tardias).
Anti-ruído: 1 mensagem por janela, dedup (kind,key) em ProactiveNotice,
cooldown por kind nos nudges, silêncio total quando não há nada.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db.models import DouSeenMP, ProactiveNotice, Reminder, User, WorkoutLog
from bot.services import jobs
from bot.services import shopping
from bot.services import tasks as tasks_svc
from bot.services.reminders import as_utc, format_reminder_line

logger = logging.getLogger(__name__)

BRT = ZoneInfo("America/Sao_Paulo")

_PROACTIVE_SYSTEM = (
    "Você é um assistente pessoal sendo PROATIVO. Reescreva os AVISOS abaixo "
    "numa ÚNICA mensagem curta e amigável em português (HTML do Telegram: "
    "<b>, emojis simples). REGRAS: use SOMENTE os fatos fornecidos; NÃO invente "
    "datas, valores ou itens; NÃO dê conselhos não pedidos; seja conciso."
)


@dataclass
class ProactiveFact:
    category: str       # 'venc' | 'mp' | 'nudge'
    kind: str           # = ProactiveNotice.kind
    key: str            # = ProactiveNotice.key
    text: str           # linha já formatada (determinística)
    date_iso: str | None = None  # MP: data de publicação no DOU (pro botão "gerar nota")


# ──────────────────────── dedup ────────────────────────

async def already_notified(session: AsyncSession, user_id: int, kind: str, key: str) -> bool:
    row = await session.scalar(
        select(ProactiveNotice.id).where(
            ProactiveNotice.user_id == user_id,
            ProactiveNotice.kind == kind,
            ProactiveNotice.key == key,
        ).limit(1)
    )
    return row is not None


async def mark_notified(session: AsyncSession, user_id: int, kind: str, key: str) -> None:
    session.add(ProactiveNotice(user_id=user_id, kind=kind, key=key))
    await session.commit()


async def unmark_notified(session: AsyncSession, user_id: int, kind: str, key: str) -> None:
    await session.execute(delete(ProactiveNotice).where(
        ProactiveNotice.user_id == user_id,
        ProactiveNotice.kind == kind,
        ProactiveNotice.key == key,
    ))
    await session.commit()


async def _nudge_recent(session: AsyncSession, user_id: int, kind: str, cooldown_days: int) -> bool:
    """True se já houve um nudge desse kind há menos de cooldown_days
    (evita repetir o mesmo nudge todo dia)."""
    last = await session.scalar(
        select(func.max(ProactiveNotice.sent_at)).where(
            ProactiveNotice.user_id == user_id, ProactiveNotice.kind == kind,
        )
    )
    if last is None:
        return False
    return (datetime.now(timezone.utc) - as_utc(last)) < timedelta(days=cooldown_days)


def parse_proactive_hours(csv: str) -> set[int]:
    """CSV de horas BRT → set[int]; inclui sempre o briefing_hour."""
    hours: set[int] = set()
    for part in (csv or "").split(","):
        part = part.strip()
        if part.isdigit() and 0 <= int(part) <= 23:
            hours.add(int(part))
    hours.add(settings.proactive_briefing_hour)
    return hours


# ──────────────────────── coletores ────────────────────────

async def collect_vencimentos(
    session: AsyncSession, user: User, now_brt: datetime, *, force: bool = False,
) -> list[ProactiveFact]:
    facts: list[ProactiveFact] = []
    tz = ZoneInfo(user.timezone)
    now_utc = now_brt.astimezone(timezone.utc)
    horizon = now_utc + timedelta(hours=settings.proactive_lookahead_hours)

    # Lembretes chegando (não recorrentes — recorrentes já disparam no horário).
    rems = (await session.scalars(
        select(Reminder).where(
            Reminder.user_id == user.id,
            Reminder.sent.is_(False),
            Reminder.recurrence.is_(None),
            Reminder.due_at > now_utc,
            Reminder.due_at <= horizon,
        ).order_by(Reminder.due_at)
    )).all()
    # Vencimentos NÃO são deduplicados: o aviso deve repetir em TODA janela até
    # o pagamento (sent=True) ou o vencimento passar. A trava run_key evita
    # repetir dentro da mesma janela.
    for r in rems:
        key = f"{r.id}:{as_utc(r.due_at).astimezone(tz).date().isoformat()}"
        facts.append(ProactiveFact("venc", "venc_rem", key,
                                    "⏳ " + format_reminder_line(r, user.timezone)))

    # Vencimento da fatura do cartão (financeiro/Firestore).
    try:
        from bot.services.financeiro import card_due_soon
        lookahead_days = max(3, settings.proactive_lookahead_hours // 24)
        res = await card_due_soon(session, user, now_brt.date(), lookahead_days)
    except Exception:
        res = None
    if res:
        facts.append(ProactiveFact(
            "venc", "card_due", res["month_key"],
            f"💳 Fatura do cartão vence em <b>{res['due_date'].strftime('%d/%m')}</b>.",
        ))
    return facts


# Checagem RETROATIVA do DOU: dia que falhou vira pendência persistente e é
# re-checado nas janelas seguintes quando o Inlabs voltar. fetch_mps cobre
# DO1E+DO1, então edição EXTRA de dia perdido entra também. Teto por janela
# (cada dia retroativo re-baixa os ZIPs do dia, ~100-200MB no Orange Pi) e
# expiração pra não insistir num dia problemático pra sempre.
_MP_RETRO_MAX_POR_JANELA = 2
_MP_RETRO_EXPIRA_DIAS = 14

# Idade máxima de uma checagem COMPLETA do dia pra ela ainda "valer" como
# contexto quando uma re-checagem falha: dentro dela, o aviso vira linha
# informativa ("chequei às HH:MM, sem MP até então"); fora, alarme forte.
# 6h = o maior vão entre janelas proativas (7→13→19): falhou a janela
# seguinte inteira sem nenhuma checagem OK no meio → volta a gritar.
_OK_RECENTE_H = 6

# Fila de NOTA TÉCNICA pendente: MP detectada, usuário pediu a nota (botão) e o
# Inlabs caiu na hora de gerar — o pedido fica na fila (kind nota_pendente,
# key "AAAA-MM-DD:num1,num2|all") e é re-tentado silenciosamente a cada janela
# até sair. Teto por janela porque a nota é LENTA (68s medidos: web search +
# LLM + DOCX) — não por risco de quota: quem garante uma geração por vez em
# todo o processo é o _SEM_NOTA (dou_monitor), inclusive entre usuários da
# casa e entre a fila e o /mp_dou_agora. Subir este número faz a fila drenar
# mais rápido sem criar concorrência: os jobs esperam no semáforo.
_NOTA_MAX_POR_JANELA = 2
_NOTA_PENDENTE_EXPIRA_DIAS = 14


async def _entregar_nota_pendente(
    bot, user_id: int, d: date, numeros: list[str] | None, key: str,
) -> None:
    """Uma re-tentativa da fila, em background e com sessão PRÓPRIA.

    Sucesso → sai da fila (a entrega do deliver_to_user É a notificação);
    Inlabs fora → silêncio, a entrada fica pra próxima janela (o usuário já
    foi avisado da fila quando pediu, e a linha de status `nota_fila` repete
    em toda janela enquanto durar)."""
    from bot.db.session import SessionLocal
    from bot.services.dou_monitor import (
        DouError, InlabsMaintenanceError, deliver_to_user,
    )

    async def _marca_manut(session, ativo: bool) -> None:
        # Reflete se a ÚLTIMA re-tentativa bateu em manutenção VERIFICADA (o
        # Inlabs literalmente diz "em manutenção"). É a única causa apurada que
        # a linha de status pode afirmar sem chutar. Setada só aqui, limpa em
        # qualquer outro desfecho — então nunca fica desatualizada.
        chave = d.isoformat()
        existe = await already_notified(session, user_id, "dou_manut", chave)
        if ativo and not existe:
            await mark_notified(session, user_id, "dou_manut", chave)
        elif existe and not ativo:
            await unmark_notified(session, user_id, "dou_manut", chave)

    async with SessionLocal() as session:
        # Usuário recarregado NA SESSÃO DO JOB: o objeto do tick pertence a uma
        # sessão que já fechou, e ORM de sessão morta estoura DetachedInstance.
        user = await session.get(User, user_id)
        if user is None or not user.is_authorized or not user.dou_mp_subscribed:
            return
        try:
            _entregues, falhas, motivo = await deliver_to_user(
                bot, session, user, d, force=True, only_numeros=numeros,
            )
        except InlabsMaintenanceError as e:
            # Causa APURADA (o Inlabs declara manutenção): a linha de status
            # passa a dizer "aguardando o Inlabs voltar (em manutenção)" em vez
            # de "gerando agora"/"próxima janela", que soam otimistas demais.
            logger.warning("nota pendente %s: Inlabs em MANUTENÇÃO (%s)", key, e)
            await _marca_manut(session, True)
            return
        except DouError as e:
            # Inlabs fora sem declarar manutenção (instabilidade genérica): não
            # afirma causa, só mantém na fila. Limpa a marca de manutenção — a
            # última tentativa não foi manutenção.
            logger.warning("nota pendente %s: Inlabs ainda fora (%s)", key, e)
            await _marca_manut(session, False)
            return
        except Exception:
            logger.exception("nota pendente %s: falha inesperada", key)
            return
        if falhas:
            # Alguma nota falhou na GERAÇÃO (Gemini 500, DOCX, send_document).
            # NÃO dar baixa: a entrada fica na fila e é re-tentada na próxima
            # janela. Baixar aqui era o buraco — a nota sumia sem re-tentativa
            # e sem aviso de desistência. Inlabs estava OK (chegou a gerar), então
            # não é manutenção.
            logger.warning("nota pendente %s: %d nota(s) falharam — mantida na fila",
                           key, len(falhas))
            await _marca_manut(session, False)
            return
        await _marca_manut(session, False)
        if _entregues == 0:
            # O Inlabs respondeu (não é fila/manutenção), mas não veio MP.
            # Distinguir o desfecho — senão a linha "gerando ... todas as MPs"
            # fica prometendo uma nota que não existe num dia sem Diário.
            if motivo in ("provisorio", "sem_mp_extra"):
                # Dia ainda em aberto — nada saiu ainda (provisorio) ou a edição
                # normal saiu sem MP mas a extra pode vir (sem_mp_extra). NÃO
                # limpa: fica na fila pra re-checar quando fechar. Silêncio: não
                # há nada iminente a prometer.
                logger.info("nota pendente %s: dia em aberto (%s), mantida na fila",
                            key, motivo)
                return
            if motivo in ("sem_edicao", "sem_mp"):
                # Desfecho DEFINITIVO sem MP: tira da fila e DIZ o que houve —
                # some em silêncio contradiz o "te envio automaticamente".
                from bot.services.dou_monitor import texto_sem_mp
                await unmark_notified(session, user_id, "nota_pendente", key)
                await _send(bot, user_id, texto_sem_mp(motivo, d) +
                            " Tirei da fila de checagem.")
                logger.info("nota pendente %s resolvida sem MP (%s)", key, motivo)
                return
            # incompleto/desconhecido: não dá pra afirmar ausência — mantém na
            # fila (na dúvida é pendência), silêncio, re-tenta na próxima janela.
            logger.info("nota pendente %s: sem baixa (motivo=%s)", key, motivo)
            return
        await unmark_notified(session, user_id, "nota_pendente", key)
        logger.info("nota pendente %s entregue", key)


async def _processar_notas_pendentes(
    bot, session: AsyncSession, user: User,
) -> list[date]:
    """Agenda as re-tentativas da fila de notas — NÃO espera por elas.

    Devolve as datas efetivamente disparadas, pro caller corrigir a linha de
    status (ver `_marcar_geradas_agora`).

    A geração leva minutos (mesmo pipeline do botão: Inlabs + pesquisa + LLM +
    DOCX). Rodando dentro do tick, segurava a sessão e o próprio tick por todo
    esse tempo, atrasando o resto da janela — inclusive lembrete que vencesse
    no meio. Aqui só a parte barata (ler a fila, expirar entrada velha) fica no
    tick; a entrega vai pra task própria, sob a MESMA chave do comando manual,
    então tick e /mp_dou_agora nunca geram a mesma nota em duplicata."""
    from bot.services.dou_monitor import chave_job_nota
    rows = list(await session.scalars(
        select(ProactiveNotice).where(
            ProactiveNotice.user_id == user.id,
            ProactiveNotice.kind == "nota_pendente",
        )
    ))
    if not rows:
        return []
    hoje = datetime.now(BRT).date()
    fila: list[tuple[date, list[str] | None, str]] = []
    for r in rows:
        date_part, _, nums = r.key.partition(":")
        try:
            d = date.fromisoformat(date_part)
        except ValueError:
            await unmark_notified(session, user.id, "nota_pendente", r.key)
            continue
        if (hoje - d).days > _NOTA_PENDENTE_EXPIRA_DIAS:
            # Desistir em silêncio contradiz o que o bot prometeu ("te envio
            # automaticamente"). Avisa antes de largar.
            await unmark_notified(session, user.id, "nota_pendente", r.key)
            await _send(bot, user.id, (
                f"⚠️ Desisti da nota técnica de {d.strftime('%d/%m')} — "
                f"{_NOTA_PENDENTE_EXPIRA_DIAS} dias sem conseguir acessar o "
                "Inlabs. Se ainda quiser, peça de novo com "
                f"/mp_dou_agora {d.strftime('%d/%m/%Y')}."
            ))
            continue
        numeros = [n for n in nums.split(",") if n and n != "all"] or None
        fila.append((d, numeros, r.key))
    # Pula quem já tem job vivo — a re-tentativa da janela anterior (ou o
    # pedido manual do dono) ainda está rodando. Sem isso a fila inteira
    # emperraria atrás dela: o teto por janela seria gasto num spawn recusado.
    prontas = [
        t for t in sorted(fila, key=lambda t: t[0])
        if not jobs.job_em_andamento(chave_job_nota(user.id, t[0]))
    ]
    disparadas: list[date] = []
    for d, numeros, key in prontas[:_NOTA_MAX_POR_JANELA]:
        if jobs.spawn(
            chave_job_nota(user.id, d),
            # Argumentos fixados por default: sem isso a lambda leria o d/key
            # do fim do laço (late binding) e re-tentaria a data errada.
            lambda d=d, numeros=numeros, key=key: _entregar_nota_pendente(
                bot, user.id, d, numeros, key,
            ),
        ):
            disparadas.append(d)
    return disparadas


async def _mp_dias_pendentes(
    session: AsyncSession, user_id: int, hoje: date, desistidos: list[date] | None = None,
) -> list[date]:
    """Dias de DOU pendentes de checagem (antigos primeiro). Limpa do banco
    pendências expiradas e chaves inválidas.

    `desistidos` (se passado) recebe os dias que expiraram — o caller vira isso
    num aviso ao usuário: desistir em silêncio de um dia não checado é
    exatamente o falso negativo que a retroativa existe pra evitar."""
    rows = list(await session.scalars(
        select(ProactiveNotice).where(
            ProactiveNotice.user_id == user_id,
            ProactiveNotice.kind == "mp_pendente",
        )
    ))
    out: list[date] = []
    for r in rows:
        try:
            d = date.fromisoformat(r.key)
        except ValueError:
            await unmark_notified(session, user_id, "mp_pendente", r.key)
            continue
        if (hoje - d).days > _MP_RETRO_EXPIRA_DIAS:
            await unmark_notified(session, user_id, "mp_pendente", r.key)
            if desistidos is not None:
                desistidos.append(d)
            continue
        out.append(d)
    return sorted(out)


async def baixa_checagem_manual(
    session: AsyncSession, user: User, d: date,
    entregues: int, falhas: list[str], motivo: str | None,
) -> bool:
    """Dá baixa nas pendências de `d` quando uma checagem MANUAL conclusiva
    (/mp_dou_agora, dia inteiro) acabou de verificar o dia.

    O comando roda o MESMO pipeline de fetch da retroativa; sem esta baixa, o
    dia seguia na fila e a janela seguinte re-baixava os ZIPs do DOU só pra
    confirmar o que o dono acabou de ver (pergunta do dono, 04/08/2026) — e
    uma nota_pendente da mesma data era re-gerada em DUPLICATA (force=True).

    Conclusiva = evidência positiva, mesma régua da retroativa (_Colheita.baixa):
    - motivo 'sem_mp'/'sem_edicao': o deliver_to_user só os devolve com fetch
      COMPLETO e dia FECHADO — houve DOU sem MP / não houve edição, definitivo;
    - entregues>0 sem falha de nota, com o dia fechado E checagem completa
      recente na memória do processo (só checagem completa entra em
      _ultima_ok; o fetch desta entrega acabou de rodar ou veio do cache de
      10 min, que também só guarda resultado completo).
    Na dúvida (incompleto, dia aberto, nota que falhou), NADA muda — a
    pendência fica, que é o lado seguro da premissa do projeto.

    Retorna True se alguma pendência foi efetivamente baixada.
    """
    from bot.services.dou_monitor import _dia_encerrado, ultima_checagem_ok

    if motivo in ("sem_mp", "sem_edicao"):
        conclusiva = True
    elif entregues > 0 and not falhas and _dia_encerrado(d):
        ult = ultima_checagem_ok(d)
        conclusiva = (ult is not None
                      and datetime.now(BRT) - ult[0] <= timedelta(minutes=15))
    else:
        conclusiva = False
    if not conclusiva:
        return False

    removidas = False
    if await already_notified(session, user.id, "mp_pendente", d.isoformat()):
        await unmark_notified(session, user.id, "mp_pendente", d.isoformat())
        removidas = True
    # nota_pendente do MESMO dia: a checagem completa acabou de fazer (ou
    # provar desnecessário) exatamente o trabalho que a fila re-tentaria.
    rows = list(await session.scalars(
        select(ProactiveNotice).where(
            ProactiveNotice.user_id == user.id,
            ProactiveNotice.kind == "nota_pendente",
        )
    ))
    for r in rows:
        if r.key.partition(":")[0] == d.isoformat():
            await unmark_notified(session, user.id, "nota_pendente", r.key)
            removidas = True
    # Marca d'água: só avança no passo CONTÍGUO. Pular dias (marca em 01/08 e
    # baixa manual de 03/08) esconderia 02/08 da _cobrir_lacuna pra sempre —
    # perda silenciosa, exatamente o que a marca existe pra impedir.
    if (user.dou_ultimo_dia_ok is not None
            and d == user.dou_ultimo_dia_ok + timedelta(days=1)):
        user.dou_ultimo_dia_ok = d
        await session.commit()
    if removidas:
        logger.info("proactive: baixa manual das pendências de %s "
                    "(motivo=%s, entregues=%d)", d, motivo, entregues)
    return removidas


async def listar_fila_mp(
    session: AsyncSession, user_id: int, hoje: date,
) -> dict:
    """Snapshot READ-ONLY da fila do monitor de MP, pro /mp_em_fila.

    Devolve:
    - 'notas': notas técnicas de MPs com NÚMERO conhecido esperando (re)geração
      (nota_pendente "DATA:1382") — lista de (data, "1382"|"1382,1383");
    - 'dias': dias que o bot ainda vai VERIFICAR — a união dos mp_pendente com
      as entradas nota_pendente "all", deduplicadas por data — (data, restantes);
    - 'ultima_ok': {data: (quando BRT, nº MPs)} da última checagem COMPLETA de
      cada dia listado em 'dias' (memória do processo; ausente = nunca checado
      OK desde o último restart). Dá contexto pro dono: "re-checo por mais N
      dias" sozinho soava como se NADA tivesse sido visto daquele dia;
    - 'manutencao': True se há aviso ativo de Inlabs em manutenção (dou_manut).

    "all" entra em 'dias', não em 'notas': é CHECAGEM (ainda não confirmou MP),
    e listá-la como "nota das MPs" prometia MP que pode não existir E duplicava o
    dia (que também está em mp_pendente). NÃO expira nem altera nada — é só
    leitura; consultar a fila não pode mudar a fila (a expiração fica no
    proativo, _mp_dias_pendentes)."""
    rows = list(await session.scalars(
        select(ProactiveNotice).where(
            ProactiveNotice.user_id == user_id,
            ProactiveNotice.kind.in_(("nota_pendente", "mp_pendente", "dou_manut")),
        )
    ))
    notas: list[tuple[date | None, str]] = []
    dias: dict[date, int] = {}
    manutencao = False

    def _add_dia(d: date, expira: int) -> None:
        restantes = expira - (hoje - d).days
        if restantes >= 0:              # expirados são varridos pelo proativo
            dias[d] = max(dias.get(d, restantes), restantes)

    for r in rows:
        if r.kind == "dou_manut":
            manutencao = True
        elif r.kind == "nota_pendente":
            d = _data_da_chave(r.key)
            _, _, nums = (r.key or "").partition(":")
            if not nums or nums == "all":
                if d is not None:       # checagem do dia → vira "dia a verificar"
                    _add_dia(d, _NOTA_PENDENTE_EXPIRA_DIAS)
            else:
                notas.append((d, nums))
        else:  # mp_pendente
            try:
                d = date.fromisoformat(r.key)
            except ValueError:
                continue
            _add_dia(d, _MP_RETRO_EXPIRA_DIAS)

    notas.sort(key=lambda t: (t[0] or date.min, t[1]))
    from bot.services.dou_monitor import _dia_encerrado, ultima_checagem_ok
    ultima_ok = {d: ok for d in dias if (ok := ultima_checagem_ok(d)) is not None}
    # Dia ABERTO (fecha às 6h do dia seguinte) × dia preso por falha: o texto
    # do /mp_fila é diferente — aberto resolve hoje/amanhã cedo (ainda há
    # checagens HOJE, ver janelas_hoje); preso é que carrega o contador de
    # desistência. Sem a distinção, o "re-checo por mais 14 dias" soava como
    # prazo esperado pro dia de HOJE (pergunta do dono, 03/08/2026).
    return {"notas": notas, "dias": sorted(dias.items()),
            "abertos": [d for d in dias if not _dia_encerrado(d)],
            "janelas_hoje": _janelas_restantes(
                datetime.now(BRT).hour, datetime.now(BRT).minute),
            "ultima_ok": ultima_ok, "manutencao": manutencao}


_ESTADO_GERANDO = "<b>gerando agora</b>, chega em alguns minutos"
# CHECAGEM (entrada 'all') NÃO promete prazo: o desfecho pode ser MP+nota,
# "não houve edição" (dia fechado) ou "ainda em aberto" (resolve quando o dia
# fechar). "em minutos" dava percepção de resposta iminente — e o silêncio no
# caso do dia aberto parecia bug (feedback do dono). O bot SEMPRE avisa o
# desfecho; o texto deixa claro que não precisa acompanhar. Sem repetir
# "checagem" (o prefixo da linha 'all' já é neutro — ver _texto_fila).
_ESTADO_CHECANDO = "<b>checando agora</b>; aviso o resultado, sem precisar acompanhar"


def _estado_em_andamento(key: str) -> str:
    """Estado 'em andamento' conforme o tipo da entrada. 'all' nasce de um
    pedido que não chegou a confirmar MP (Inlabs fora) → é CHECAGEM, não geração
    de nota; dizer 'gerando a nota' prometeria MP que pode não existir."""
    _, _, nums = (key or "").partition(":")
    return _ESTADO_CHECANDO if (not nums or nums == "all") else _ESTADO_GERANDO


def _fmt_hora_janela(h: int) -> str:
    """'13h05' (PROACTIVE_MINUTE=5) ou '13h' (offset 0). O minuto aparece nos
    textos porque as janelas saíram da hora redonda de propósito (fugir do
    pico do Inlabs) — dizer 'às 13h' com disparo às 13h05 geraria a pergunta
    'por que atrasou?'."""
    m = settings.proactive_minute
    return f"{h}h{m:02d}" if m else f"{h}h"


def _janelas_restantes(hora: int, minuto: int | None = None) -> list[int]:
    """Horas das janelas proativas DE HOJE ainda por vir após o instante.

    Com `minuto`, a janela da hora CORRENTE ainda conta se o disparo
    (PROACTIVE_MINUTE) não passou — sem isso, entre 13h00 e 13h04 o /mp_fila
    dizia que a próxima checagem era só às 19h, com a das 13h05 a minutos de
    distância. `minuto=None` mantém o comportamento conservador (hora
    corrente já não conta)."""
    inclui_atual = minuto is not None and minuto < settings.proactive_minute
    return sorted(
        h for h in parse_proactive_hours(settings.proactive_hours)
        if h > hora or (h == hora and inclui_atual)
    )


def _checado_sem_mp_dia_aberto(key: str, agora: datetime | None = None) -> bool:
    """True quando dá pra AFIRMAR o estado apurado de uma entrada 'all': o dia
    foi checado COMPLETO, sem MP, e segue aberto. False quando não dá — sem
    checagem OK registrada (restart/Inlabs fora), MP encontrada, dia já
    fechado, ou entrada com números (essa é nota em geração, não checagem).

    Pedido do dono (03/08/2026, em rodadas): "checando agora; aviso o
    resultado" descrevia PROCESSO onde dava pra descrever ESTADO — e, apurado
    o estado, repeti-lo em TODA janela é ruído. Com True, a linha da FILA é
    omitida (a entrada não é trabalho pendente, é espera): quem fala pelo dia
    é o BATIMENTO em collect_mp — abertura no briefing, fechamento na última
    janela com a ressalva da extra tardia (o dia só fecha às 6h; o veredito
    vem no briefing, que resolve a entrada com "Tirei da fila"). Mudança de
    estado fala por si: MP achada vira aviso próprio, falha vira o aviso de
    2 estágios, e o /mp_fila mostra o "já checado" a qualquer hora."""
    from bot.services.dou_monitor import _dia_encerrado, ultima_checagem_ok
    d = _data_da_chave(key)
    _, _, nums = (key or "").partition(":")
    if d is None or (nums and nums != "all"):
        return False
    ok = ultima_checagem_ok(d)
    return ok is not None and ok[1] == 0 and not _dia_encerrado(d, agora)


def _texto_fila(key: str, estado: str) -> str:
    """Linha de status de uma entrada da fila. Compartilhada entre a montagem
    (collect_mp) e a correção pós-disparo (_marcar_geradas_agora) — texto
    duplicado nos dois lugares sairia do ar um dia sem ninguém notar.

    'all' (pedido que não chegou a confirmar MP — Inlabs fora) vira CHECAGEM do
    DOU: falar em 'nota das MPs' promete MP que pode não existir (num domingo,
    p.ex., era exatamente o texto enganoso 'gerando ... todas as MPs de 02/08').
    Só com os NÚMEROS conhecidos é 'nota técnica da MP X'."""
    d = _data_da_chave(key)
    _, _, nums = (key or "").partition(":")
    quando = d.strftime("%d/%m") if d else "?"
    if not nums or nums == "all":
        # Sujeito NEUTRO ("DOU de DD/MM"): compõe com qualquer estado (checando,
        # aguardando a vez, Inlabs fora…) sem repetir "checagem/checando", que
        # era o que deixava a linha torta ("Checagem do DOU… checando").
        return f"📄 DOU de {quando} — {estado}."
    return f"📄 Nota técnica (MP {nums.replace(',', ', ')} de {quando}) — {estado}."


def _marcar_geradas_agora(facts: list[ProactiveFact], disparadas: list[date]) -> None:
    """Corrige as linhas cuja nota COMEÇOU a ser gerada nesta execução.

    A linha nasce em `collect_mp`, antes do disparo, então diria "tento na
    próxima janela" pra uma nota que já está rodando. Corrigir DEPOIS (em vez
    de disparar antes de coletar) é deliberado: adiantar o disparo poria o
    fetch do job concorrendo com o fetch do coletor na MESMA data — dois
    downloads do Inlabs no Orange Pi e, pior, a MP podendo aparecer duas vezes
    (uma na mensagem da janela, outra na entrega do job, que ainda não marcou
    como vista). A mensagem só é composta adiante, então dá tempo.
    """
    if not disparadas:
        return
    alvo = set(disparadas)
    for i, f in enumerate(facts):
        if f.kind == "nota_fila" and _data_da_chave(f.key) in alvo:
            # Estado APURADO não é rebaixado: dia já checado sem MP e aberto →
            # a linha (quando existe — só na última janela) já diz o que se
            # sabe; trocá-la por "checando agora" (o job disparado resolve em
            # milissegundos via cache e mantém na fila em silêncio) seria
            # trocar informação por processo.
            if _checado_sem_mp_dia_aberto(f.key):
                continue
            facts[i] = replace(f, text=_texto_fila(f.key, _estado_em_andamento(f.key)))


def _data_da_chave(key: str) -> date | None:
    """Data da chave "AAAA-MM-DD:nums". None quando a chave está corrompida."""
    try:
        return date.fromisoformat(key.partition(":")[0])
    except ValueError:
        return None


async def _cobrir_lacuna(
    session: AsyncSession, user: User, hoje: date,
) -> list[ProactiveFact]:
    """Transforma em pendência os dias que o bot NUNCA olhou.

    A pendência retroativa só nascia de uma tentativa que FALHOU. Dia em que o
    bot sequer rodou (container fora, queda de luz no Orange Pi, deploy longo,
    fim de semana com a máquina desligada) não deixava rastro: na volta ele
    olhava só hoje (+ontem no briefing) e o resto sumia em silêncio — sem
    pendência, sem aviso, sem ninguém pra notar.

    A marca d'água (`dou_ultimo_dia_ok`) fecha isso: tudo entre ela e ontem
    que não foi checado entra na fila retroativa.
    """
    marca = user.dou_ultimo_dia_ok
    if marca is None:
        # Primeira janela com a coluna: adota ontem, sem varrer o passado.
        # Enfileirar 14 dias de uma vez custaria um fetch de ~6s cada e
        # inundaria o dono de avisos sobre dias que ele nunca esperou.
        user.dou_ultimo_dia_ok = hoje - timedelta(days=1)
        await session.commit()
        return []

    # Hoje fica de fora: está sendo checado nesta janela.
    lacuna = [marca + timedelta(days=i) for i in range(1, (hoje - marca).days)]
    if not lacuna:
        return []

    limite = hoje - timedelta(days=_MP_RETRO_EXPIRA_DIAS)
    for d in (d for d in lacuna if d >= limite):
        if not await already_notified(session, user.id, "mp_pendente", d.isoformat()):
            await mark_notified(session, user.id, "mp_pendente", d.isoformat())
    logger.warning("proactive: lacuna de %d dia(s) no DOU (marca=%s, hoje=%s)",
                   len(lacuna), marca, hoje)

    # Dia velho demais pra retroativa NÃO pode sumir calado — é justamente o
    # caso em que o bot ficou fora por muito tempo e mais provavelmente perdeu
    # MP. Avisa uma vez, com as datas, e diz o que fazer.
    facts: list[ProactiveFact] = []
    velhos = [d for d in lacuna if d < limite]
    if velhos:
        key = f"lacuna:{velhos[0].isoformat()}:{velhos[-1].isoformat()}"
        if not await already_notified(session, user.id, "mp_lacuna", key):
            periodo = (
                velhos[0].strftime("%d/%m") if len(velhos) == 1
                else f"{velhos[0].strftime('%d/%m')} a {velhos[-1].strftime('%d/%m')}"
            )
            facts.append(ProactiveFact(
                "mp", "mp_lacuna", key,
                f"⚠️ <b>Fiquei sem checar o DOU</b> de {periodo} "
                f"({len(velhos)} dia(s)) — passou dos {_MP_RETRO_EXPIRA_DIAS} "
                "dias da re-checagem automática. Esses dias NÃO foram "
                "verificados; se precisar, rode "
                f"<code>/mp_dou_agora {velhos[0].strftime('%d/%m/%Y')}</code>.",
                date_iso=None,
            ))

    # A lacuna está contabilizada (na fila ou avisada): a marca avança pra não
    # re-enfileirar tudo na próxima janela.
    user.dou_ultimo_dia_ok = hoje - timedelta(days=1)
    await session.commit()
    return facts


async def _conferir_camara(
    session: AsyncSession, user: User, hoje: date,
) -> list[ProactiveFact]:
    """Confere o que o bot entregou contra a lista de MPs da Câmara.

    Toda a detecção depende do Inlabs, e o pior modo de falha dele é mudo:
    404 em arquivo que existe, ZIP truncado servido como válido. O bot conclui
    "não houve MP" e nada no estado dele denuncia o buraco. A Câmara é outro
    órgão e outra infraestrutura — é o único jeito de saber o que se perdeu.

    Achando MP não entregue, enfileira o DIA dela como pendência: a retroativa
    que já existe busca no DOU e entrega com nota, sem caminho novo de entrega.
    """
    from bot.services.dou_monitor import (
        _JANELA_CONFERENCIA_DIAS, mps_nao_recebidas,
    )
    try:
        faltando = await mps_nao_recebidas(session, user.id, hoje)
    except Exception as exc:
        # Conferência que falha em silêncio é pior que conferência nenhuma:
        # passa a sensação de cobertura que não existe. Avisa 1x/dia (a API da
        # Câmara tem 502/504 transitório; alarme a cada janela seria ruído).
        logger.warning("proactive: conferência com a Câmara falhou: %s", exc)
        key = f"conf_fail:{hoje.isoformat()}"
        if await already_notified(session, user.id, "mp_conf_fail", key):
            return []
        return [ProactiveFact(
            "mp", "mp_conf_fail", key,
            "⚠️ Não consegui conferir as MPs com a Câmara hoje (API fora). "
            "A checagem do DOU seguiu normal; só a conferência cruzada ficou "
            "de fora desta rodada.",
            date_iso=None,
        )]

    facts: list[ProactiveFact] = []
    for mp in faltando:
        key = f"conf:{mp['numero']}/{mp['ano']}"
        if await already_notified(session, user.id, "mp_conferencia", key):
            continue
        d = mp["data"]
        recuperavel = (hoje - d).days <= _MP_RETRO_EXPIRA_DIAS
        if recuperavel and not await already_notified(
            session, user.id, "mp_pendente", d.isoformat()
        ):
            await mark_notified(session, user.id, "mp_pendente", d.isoformat())
        ementa = _clean_ementa(mp.get("ementa") or "")
        if recuperavel:
            saida = ("Já coloquei o dia na fila — vou buscar no DOU e te "
                     "mandar com a nota técnica.")
        else:
            saida = (f"Passou dos {_MP_RETRO_EXPIRA_DIAS} dias da re-checagem "
                     f"automática; pra recuperar, rode "
                     f"<code>/mp_dou_agora {d.strftime('%d/%m/%Y')}</code>.")
        facts.append(ProactiveFact(
            "mp", "mp_conferencia", key,
            f"⚠️ <b>MP {mp['numero']}/{mp['ano']}</b> ({d.strftime('%d/%m')}) "
            f"saiu e você NÃO recebeu — achei conferindo com a Câmara. "
            f"{ementa} {saida}",
            date_iso=None,
        ))
    if faltando and not facts:
        logger.info("proactive: conferência — %d MP(s) faltando, todas já "
                    "avisadas antes", len(faltando))
    logger.info("proactive: conferência com a Câmara ok (janela de %d dias)",
                _JANELA_CONFERENCIA_DIAS)
    return facts


@dataclass
class _Colheita:
    """Resultado de checar UM dia de DOU.

    É um objeto (e não a tupla que era antes) de propósito: a tupla foi a
    causa de um bug que derrubava a janela proativa inteira — um call site
    fazia `facts += await _colher(d)` e continuou "funcionando" quando a tupla
    ganhou um segundo elemento, contaminando a lista de fatos. Com atributos
    nomeados, um campo novo não passa despercebido por call site nenhum.
    """

    facts: list[ProactiveFact]
    completo: bool      # nenhuma seção FALHOU (erro de rede, ZIP inválido…)
    provisorio: bool    # 404 numa seção com o dia ainda aberto
    sem_edicao: bool = False   # nenhuma fonte de Seção 1 na listagem do dia
    mps_no_dia: int = 0        # MPs no DOU do dia (BRUTO, antes de dedup)

    @property
    def baixa(self) -> bool:
        """Se o dia pode ser dado como checado. Um lugar só decide isso."""
        return self.completo and not self.provisorio


async def collect_mp(
    session: AsyncSession, user: User, dates: list[date], *,
    force: bool = False, conferir: bool = False,
) -> list[ProactiveFact]:
    if not user.dou_mp_subscribed:
        return []
    from bot.services.dou_monitor import fetch_mps
    facts: list[ProactiveFact] = []
    seen: set[str] = set()
    failed: list[date] = []
    provisorios: list[date] = []
    # (numero, ano) já ENTREGUES com nota (dou_seen_mps) — carregado só
    # quando aparece MP (lazy). É a mesma semântica da conferência com a
    # Câmara: "o dono FICOU SABENDO?" = aviso do proativo OU nota entregue.
    # Sem a união, toda MP entregue via /mp_dou_agora era RE-ANUNCIADA na
    # janela seguinte com botão de gerar a nota de novo — duplicata
    # sistemática, não caso de dúvida.
    entregues: set | None = None

    async def _colher(d: date) -> _Colheita:
        """Colheita das MPs de um dia (dedup por número e por
        já-notificada). `completo=False` quando uma seção do DOU falhou: o que
        veio é entregue, mas o dia NÃO recebe baixa da pendência.
        Levanta exceção quando o fetch falha inteiro — o caller decide."""
        nonlocal entregues
        mps = await fetch_mps(d)
        completo = not getattr(mps, "incompleto", False)
        provisorio = bool(getattr(mps, "provisorio", False))
        out: list[ProactiveFact] = []
        for mp in mps:
            key = f"{mp['numero']}/{mp['ano']}"
            if key in seen:
                continue
            seen.add(key)
            if not force and await already_notified(session, user.id, "mp", key):
                continue
            if not force:
                if entregues is None:
                    rows_seen = await session.scalars(
                        select(DouSeenMP).where(DouSeenMP.user_id == user.id)
                    )
                    entregues = {(r.numero, r.ano) for r in rows_seen}
                if (mp["numero"], mp["ano"]) in entregues:
                    continue
            ementa = _clean_ementa(mp.get("ementa") or "")
            out.append(ProactiveFact(
                "mp", "mp", key,
                f"📜 MP {mp['numero']}/{mp['ano']}: {ementa}",
                date_iso=d.isoformat(),
            ))
        return _Colheita(out, completo, provisorio,
                         bool(getattr(mps, "sem_edicao", False)), len(mps))

    # ANTES de varrer: dias que o bot nunca olhou viram pendência (marca
    # d'água). Sem isso, o que ele perdeu enquanto esteve fora é invisível.
    hoje_ = datetime.now(BRT).date()
    facts += await _cobrir_lacuna(session, user, hoje_)
    # Conferência com a Câmara: 1x/dia (briefing), não a cada janela — é uma
    # rede de segurança, não uma fonte primária, e a MP recém-publicada leva
    # até 1 dia pra aparecer lá.
    if conferir:
        facts += await _conferir_camara(session, user, hoje_)

    ok_dates: set[date] = set()
    inlabs_fora = False   # fetch RAISOU este run → Inlabs inacessível agora
    colheita_hoje: _Colheita | None = None
    for d in dates:
        # CURTO-CIRCUITO: o primeiro fetch que falhou já provou que o Inlabs
        # está fora AGORA — insistir nas datas seguintes só repete a cascata
        # de timeouts (login 3x + retries = minutos por data) segurando o
        # tick inteiro, com o mesmo desfecho. As datas puladas viram
        # pendência igual às falhas (re-checadas quando ele voltar).
        if inlabs_fora:
            failed.append(d)
            continue
        try:
            c = await _colher(d)
            if d == hoje_:
                colheita_hoje = c
            facts += c.facts
            if c.baixa:
                ok_dates.add(d)
            elif not c.completo:
                # Seção falhou: o que veio é entregue, mas o dia NÃO recebe
                # baixa — fica pendente pra re-checagem quando o Inlabs voltar
                # (senão MP só da edição Extra sumiria em silêncio).
                logger.warning("proactive: %s veio INCOMPLETO; mantendo pendência", d)
                failed.append(d)
            else:
                # Só 404 com o dia aberto: pendência SEM aviso (não houve
                # falha — a seção pode simplesmente ainda não ter saído).
                provisorios.append(d)
        except Exception as exc:
            logger.warning("proactive: fetch_mps(%s) falhou: %s", d, exc)
            failed.append(d)
            inlabs_fora = True

    # Sinaliza pro run_for_user: se o fetch DESTE run não alcançou o Inlabs, não
    # adianta disparar job de nota (ele buscaria o mesmo Inlabs e falharia) — e
    # dizer "gerando agora" seria mentira, já que a própria checagem acima
    # provou que o Inlabs está fora. Atributo transiente (não é coluna).
    user.dou_fora_agora = inlabs_fora

    # BATIMENTO da checagem do dia (pedido do dono, 03/08/2026): confirmação
    # POSITIVA de que o DOU de hoje foi checado — silêncio não serve como
    # evidência (é indistinguível de "não checou"). Fala DUAS vezes por dia:
    # no briefing (abre o dia) e na última janela (fecha o dia, com a ressalva
    # da extra tardia — o dia só encerra às 6h); nas janelas do meio, silêncio
    # (o dono pediu: apurado o estado, repetir é ruído). Só afirma o que foi
    # apurado: fetch COMPLETO e 0 MP no dia (com MP, as linhas de MP são a
    # evidência; com falha/incompleto, quem fala é o aviso de 2 estágios).
    # Distingue "chequei, sem MP" de "chequei e a edição nem saiu" — às 7h o
    # DOU costuma ainda não estar no Inlabs, e afirmar "sem MP" aí seria mais
    # do que se sabe.
    if colheita_hoje is not None and colheita_hoje.completo \
            and colheita_hoje.mps_no_dia == 0:
        agora_ = datetime.now(BRT)
        hora_agora = agora_.hour
        restantes = _janelas_restantes(hora_agora, agora_.minute)
        if conferir and restantes:
            # Abertura do dia (briefing/força): diz o estado e quando re-checa.
            quando = " e às ".join(_fmt_hora_janela(h) for h in restantes)
            situacao = ("ainda sem edição publicada" if colheita_hoje.sem_edicao
                        else "sem MP até o momento")
            facts.append(ProactiveFact(
                "mp", "mp_checagem", f"{hoje_.isoformat()}:abre",
                f"📄 DOU de hoje: {situacao} — re-checo às {quando}.",
                date_iso=None,
            ))
        elif not restantes:
            # Fechamento do dia (última janela): a palavra final de hoje, sem
            # afirmar veredito — extra tardia existe e o briefing resolve.
            if colheita_hoje.sem_edicao:
                texto = (f"📄 DOU de hoje: sem edição publicada até as "
                         f"{_fmt_hora_janela(hora_agora)} — se sair alguma, "
                         "chega no briefing de amanhã.")
            else:
                texto = (f"📄 DOU de hoje: sem MP na checagem das "
                         f"{_fmt_hora_janela(hora_agora)} — extra tardia (se "
                         "houver) chega no briefing de amanhã.")
            facts.append(ProactiveFact(
                "mp", "mp_checagem", f"{hoje_.isoformat()}:fecha", texto,
                date_iso=None,
            ))

    # Dia que falhou vira PENDÊNCIA persistente — gravada JÁ (não no pós-envio):
    # precisa sobreviver mesmo que o envio desta janela falhe. Provisório entra
    # na mesma fila: a diferença entre os dois é só o aviso, logo abaixo.
    for d in failed + provisorios:
        if not await already_notified(session, user.id, "mp_pendente", d.isoformat()):
            await mark_notified(session, user.id, "mp_pendente", d.isoformat())

    # CRÍTICO: se NÃO conseguiu checar o DOU, AVISA — senão o usuário vê o
    # briefing sem MP e conclui (errado) que não houve MP publicada.
    #
    # DOIS ESTÁGIOS (03/08/2026): o alarme forte ("NÃO assuma que não houve
    # MP") disparava minutos depois de o /mp_dou_agora ter checado o MESMO dia
    # com sucesso e respondido "nenhuma MP" — as duas frases, lado a lado,
    # minavam a confiança no monitor. Alarme que grita à toa treina o dono a
    # ignorar, e isso perde MP tão bem quanto o silêncio. Agora:
    # - houve checagem COMPLETA do dia há pouco (≤ _OK_RECENTE_H) → linha
    #   INFORMATIVA com o contexto apurado ("chequei às HH:MM, sem MP até
    #   então"), 1x por data;
    # - sem checagem OK recente → alarme forte, como antes. A checagem OK
    #   "envelhecendo" (> _OK_RECENTE_H) reescala pro forte sozinha.
    # A falha nunca deixa de ser DITA (premissa: falha ≠ silêncio) — muda só o
    # tom, conforme o que o bot consegue AFIRMAR. A pendência de re-checagem
    # independe do aviso (já foi gravada acima). date_iso=None mantém os avisos
    # FORA do botão de nota técnica (não são MP de verdade).
    if failed:
        from bot.services.dou_monitor import ultima_checagem_ok
        agora_brt = datetime.now(BRT)
        fortes: list[date] = []
        for d in failed:
            ok = ultima_checagem_ok(d)
            if ok is None or (agora_brt - ok[0]) > timedelta(hours=_OK_RECENTE_H):
                fortes.append(d)
                continue
            skey = f"failsoft:{d.isoformat()}"
            if not force and await already_notified(session, user.id, "mp_fail", skey):
                continue
            quando, n_mps = ok
            ate_entao = (f"{n_mps} MP(s) detectada(s) até então" if n_mps
                         else "sem MP até então")
            facts.append(ProactiveFact(
                "mp", "mp_fail", skey,
                f"ℹ️ DOU de {d.strftime('%d/%m')}: chequei às "
                f"{quando.strftime('%H:%M')} ({ate_entao}); a re-checagem de "
                "agora falhou — sigo re-checando sozinho e te aviso se vier MP.",
                date_iso=None,
            ))
        if fortes:
            fkey = "fail:" + ",".join(sorted(d.isoformat() for d in fortes))
            if force or not await already_notified(session, user.id, "mp_fail", fkey):
                datas = ", ".join(d.strftime("%d/%m") for d in fortes)
                facts.append(ProactiveFact(
                    "mp", "mp_fail", fkey,
                    f"⚠️ <b>Não consegui checar o DOU</b> de {datas} (Inlabs "
                    "instável). NÃO assuma que não houve MP — confira depois com "
                    "<code>/mp_dou_agora</code>.",
                    date_iso=None,
                ))

    # Checagem RETROATIVA dos dias pendentes de janelas anteriores. A pendência
    # só é limpa APÓS o envio (run() → mp_retro), então falha de envio não
    # perde o dia. Dia pendente que já entrou na varredura normal desta janela
    # (ex.: briefing re-checa ontem) conta como coberto, sem novo fetch.
    hoje = datetime.now(BRT).date()
    desistidos: list[date] = []
    pendentes = await _mp_dias_pendentes(session, user.id, hoje, desistidos)
    for d in desistidos:
        # Aviso explícito ao desistir: sem isto o dia sumia da fila em silêncio
        # e o usuário jamais saberia que aquele DOU nunca foi verificado.
        facts.append(ProactiveFact(
            "mp", "mp_desisti", f"desisti:{d.isoformat()}",
            f"⚠️ Desisti de checar o DOU de {d.strftime('%d/%m')} — "
            f"{_MP_RETRO_EXPIRA_DIAS} dias sem conseguir acessar o Inlabs. "
            f"Esse dia NÃO foi verificado; se quiser, rode "
            f"/mp_dou_agora {d.strftime('%d/%m/%Y')}.",
            date_iso=None,
        ))
    resolvidos: list[date] = [d for d in pendentes if d in ok_dates]
    restantes = [d for d in pendentes if d not in dates][:_MP_RETRO_MAX_POR_JANELA]
    for d in restantes:
        # Mesmo curto-circuito da varredura: Inlabs fora neste run → não paga
        # a cascata de timeouts de novo; os dias seguem pendentes.
        if inlabs_fora:
            logger.info("proactive: Inlabs fora neste run — retroativa de %s adiada", d)
            break
        try:
            c = await _colher(d)
        except Exception as exc:
            logger.warning("proactive: retroativa DOU %s ainda falhando: %s", d, exc)
            inlabs_fora = True
            continue
        facts += c.facts
        if not c.baixa:
            # Incompleta (seção falhou) ou provisória (404 com o dia ainda
            # aberto): entrega o que veio, mas o dia NÃO recebe baixa —
            # continua pendente, igual à varredura normal. Dar baixa aqui
            # perderia a edição Extra em silêncio.
            logger.warning("proactive: retroativa %s sem baixa (completo=%s "
                           "provisorio=%s)", d, c.completo, c.provisorio)
            continue
        resolvidos.append(d)
    for d in sorted(resolvidos):
        novas = sum(1 for f in facts if f.kind == "mp" and f.date_iso == d.isoformat())
        detalhe = f"{novas} MP(s) nova(s) acima" if novas else "nenhuma MP nova"
        facts.append(ProactiveFact(
            "mp", "mp_retro", f"retro:{d.isoformat()}",
            f"✅ Checagem retroativa do DOU de {d.strftime('%d/%m')} concluída — {detalhe}.",
            date_iso=None,
        ))

    # A retroativa pode ter descoberto o Inlabs fora DEPOIS da atribuição
    # inicial — re-sincroniza o sinal que trava o disparo de jobs de nota.
    user.dou_fora_agora = inlabs_fora

    # Marca d'água avança com o dia mais recente que recebeu baixa. É o que
    # permite detectar a lacuna na próxima volta — sem isso ela congelaria em
    # "ontem" e um período fora do ar de 3 dias viraria só 1 dia de pendência.
    checados = ok_dates | set(resolvidos)
    if checados:
        novo = max(checados)
        if user.dou_ultimo_dia_ok is None or novo > user.dou_ultimo_dia_ok:
            user.dou_ultimo_dia_ok = novo
            await session.commit()

    # NOTAS na fila (pedidas com o Inlabs fora): linha de status em TODA janela
    # (kind nota_fila não é dedupado no run) até a entrega dar baixa.
    rows = list(await session.scalars(
        select(ProactiveNotice).where(
            ProactiveNotice.user_id == user.id,
            ProactiveNotice.kind == "nota_pendente",
        )
    ))
    # O texto diz o que o bot OBSERVA agora, não por que a entrada foi criada.
    # Antes afirmava "Inlabs instável" sempre — e a causa não fica registrada
    # em lugar nenhum, então a frase era chute: seguia dizendo isso com o
    # Inlabs de pé e a nota já sendo gerada na mesma rodada.
    from bot.services.dou_monitor import chave_job_nota
    # Datas cuja ÚLTIMA re-tentativa bateu em manutenção verificada (kind
    # dou_manut) — permite a linha dizer a causa APURADA em vez de otimismo.
    manut_rows = list(await session.scalars(
        select(ProactiveNotice).where(
            ProactiveNotice.user_id == user.id,
            ProactiveNotice.kind == "dou_manut",
        )
    ))
    em_manutencao = {r.key for r in manut_rows}
    fila_ordenada = sorted(
        (r for r in rows if _data_da_chave(r.key)),
        key=lambda r: _data_da_chave(r.key),
    )
    for pos, r in enumerate(fila_ordenada):
        d = _data_da_chave(r.key)
        # Estado APURADO primeiro (dia checado COMPLETO, sem MP, ainda aberto).
        # Manutenção/Inlabs fora vencem — nesses a checagem desta janela NÃO
        # aconteceu, e o registro seria de horas atrás (não vale afirmar
        # "sem MP até o momento").
        apurado = (d.isoformat() not in em_manutencao and not inlabs_fora
                   and _checado_sem_mp_dia_aberto(r.key))
        if apurado:
            # Dia já checado COMPLETO, sem MP, ainda aberto: a entrada não é
            # trabalho pendente, é só espera — nada a dizer AQUI (pedido do
            # dono: apurado o estado, repetir é ruído). Quem fala pelo dia é o
            # BATIMENTO (collect_mp): abertura no briefing e fechamento na
            # última janela. Mudança de estado fala por si: MP achada vira
            # aviso próprio, falha vira o aviso de 2 estágios, e o /mp_fila
            # mostra o "já checado" a qualquer hora.
            continue
        elif d.isoformat() in em_manutencao:
            estado = ("<b>na fila de checagem</b> — o Inlabs está em manutenção; "
                      "checo e envio quando ele voltar (pode não ser hoje)")
        elif inlabs_fora:
            # A checagem DESTE run não alcançou o Inlabs: a nota não gera agora.
            # NÃO dizer "gerando"/"assim que responder" — soa iminente e o dono
            # esquece. "instável" (não "fora"): quase sempre é RECUSA DE SESSÃO
            # transitória, não o site caído — o dono vê o Inlabs de pé no
            # navegador e "fora" soa como bug. Sem promessa de prazo.
            estado = ("<b>na fila de checagem</b> — o Inlabs está instável agora "
                      "(recusando a sessão); checo e envio assim que estabilizar")
        elif jobs.job_em_andamento(chave_job_nota(user.id, d)):
            estado = _estado_em_andamento(r.key)
        elif pos >= _NOTA_MAX_POR_JANELA:
            estado = (f"aguardando a vez (gero até {_NOTA_MAX_POR_JANELA} por "
                      "janela) — envio assim que sair")
        else:
            estado = "tento na próxima janela e envio assim que sair"
        facts.append(ProactiveFact(
            "mp", "nota_fila", r.key, _texto_fila(r.key, estado), date_iso=None,
        ))
    return facts


def _clean_ementa(ementa: str, limit: int = 220) -> str:
    """Limpa a ementa pro aviso leve: remove o TÍTULO do próprio ato que às
    vezes vem anexado no fim ('... MEDIDA PROVISÓRIA Nº 1.371, DE 22 DE JUNHO
    DE 2026 ...') e trunca em limite com '…'.

    O título anexado é MAIÚSCULO e datado. Uma menção a OUTRA MP DENTRO da
    ementa ('Altera a Medida Provisória nº 1.354, de 30 de abril...') vem em
    caixa-título/minúscula e NÃO pode cortar — antes, com IGNORECASE, cortava
    nela e a ementa virava só 'Altera a'."""
    e = re.sub(r"\s+", " ", ementa).strip()
    # casa só o título anexado: MAIÚSCULO + número + ', DE <dia>' (case-sensitive)
    cut = re.search(r"MEDIDA\s+PROVIS[ÓO]RIA\s+N\S*\s*[\d.]+,?\s+DE\s+\d", e)
    if cut and cut.start() > 0:
        e = e[:cut.start()].strip()
    if len(e) > limit:
        e = e[:limit].rsplit(" ", 1)[0].rstrip(" .,;") + "…"
    return e


async def collect_nudges(
    session: AsyncSession, user: User, now_brt: datetime, *, force: bool = False,
) -> list[ProactiveFact]:
    facts: list[ProactiveFact] = []
    today = now_brt.date()
    cooldown = settings.proactive_nudge_cooldown_days

    async def _ok(kind: str) -> bool:
        if force:
            return True
        key = today.isoformat()
        if await already_notified(session, user.id, kind, key):
            return False
        return not await _nudge_recent(session, user.id, kind, cooldown)

    # Treino parado.
    last_w = await session.scalar(select(func.max(WorkoutLog.date)).where(WorkoutLog.user_id == user.id))
    if last_w is not None:
        dias = (today - last_w).days
        if dias >= settings.proactive_workout_idle_days and await _ok("nudge_workout"):
            facts.append(ProactiveFact("nudge", "nudge_workout", today.isoformat(),
                                       f"🏋️ Você não registra treino há {dias} dias."))

    # Lançamentos financeiros parados.
    try:
        from bot.services.financeiro import last_finance_activity
        last_f = await last_finance_activity(session, user)
    except Exception:
        last_f = None
    if last_f is not None:
        dias = (today - last_f).days
        if dias >= settings.proactive_finance_idle_days and await _ok("nudge_finance"):
            facts.append(ProactiveFact("nudge", "nudge_finance", today.isoformat(),
                                       f"💸 Faz {dias} dias que você não lança nada no financeiro."))

    # Lista de compras parada.
    items = await shopping.list_items(session, user.id, only_pending=True)
    if items:
        oldest = as_utc(min(i.created_at for i in items))
        dias = (today - oldest.astimezone(ZoneInfo(user.timezone)).date()).days
        if dias >= settings.proactive_shopping_idle_days and await _ok("nudge_shopping"):
            n = len(items)
            facts.append(ProactiveFact("nudge", "nudge_shopping", today.isoformat(),
                                       f"🛒 Sua lista de compras tem {n} item(ns) parado(s) há {dias} dias."))
    return facts


_TASKS_LIMIT = 12  # teto de tarefas na mensagem (evita briefing gigante)


async def collect_tarefas(
    session: AsyncSession, user: User, now_brt: datetime,
) -> list[ProactiveFact]:
    """Tarefas abertas (/tarefas) pro briefing matinal e o resumo do fim do
    dia — lembrete pra não esquecer. Sem dedup: repete até o usuário concluir.
    Mostra idade em dias pra dar relevo às que estão paradas; corta no teto."""
    tarefas = await tasks_svc.list_open_tasks(session, user.id)
    if not tarefas:
        return []
    tz = ZoneInfo(user.timezone)
    today = now_brt.date()
    facts: list[ProactiveFact] = []
    for t in tarefas[:_TASKS_LIMIT]:
        dias = (today - as_utc(t.created_at).astimezone(tz).date()).days
        idade = f"  <i>(há {dias}d)</i>" if dias >= 1 else ""
        facts.append(ProactiveFact("tarefas", "tarefa", str(t.id), f"• {t.text}{idade}"))
    extra = len(tarefas) - _TASKS_LIMIT
    if extra > 0:
        facts.append(ProactiveFact("tarefas", "tarefa_more", "more",
                                   f"… e mais {extra} tarefa(s) — veja em /tarefas"))
    return facts


async def collect_clima(
    session: AsyncSession, user: User, now_brt: datetime,
) -> list[ProactiveFact]:
    """Previsão do tempo do dia (Google Weather → Open-Meteo) pro briefing. Em MODO
    VIAGEM ativo, usa as coords/fuso do DESTINO (com rótulo); senão HOME_COORDS.
    Roda todo dia; sem dedup (leitura fresca); falha não derruba o briefing.

    Falha e falta de configuração são DITAS, não engolidas: os dois caminhos
    devolviam lista vazia, e briefing sem linha de clima é indistinguível de
    briefing com clima que não deu notícia — o dono não tinha como saber que
    devia haver algo ali."""
    from bot.services.viagem import effective_coords, effective_tz
    coords = effective_coords(user)
    label = f" em {user.viagem_destino}" if coords else ""
    tz = effective_tz(user) if coords else settings.timezone
    if not coords:
        coords = settings.home_coords
    if not coords:
        # Falta de configuração é permanente: avisa UMA vez (dedup pelo kind)
        # em vez de sumir pra sempre ou repetir todo dia. Marcado AQUI: o
        # pós-envio do run pula a categoria 'clima' de propósito (a linha
        # normal repete todo briefing), então o "1x" nunca era marcado e o
        # aviso repetia diariamente.
        logger.warning("proactive: sem HOME_COORDS — briefing sai sem clima")
        if await already_notified(session, user.id, "clima_sem_coords", ""):
            return []
        await mark_notified(session, user.id, "clima_sem_coords", "")
        return [ProactiveFact(
            "clima", "clima_sem_coords", "",
            "ℹ️ O briefing está saindo <b>sem previsão do tempo</b>: falta "
            "<code>HOME_COORDS</code> no .env (ex.: <code>-15.79,-47.88</code>). "
            "Configurando, a linha do clima volta sozinha.",
        )]
    import httpx
    from bot.services.weather import fetch_today_weather, format_weather_line
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            w = await fetch_today_weather(client, coords, tz=tz)
    except Exception as exc:
        # Silêncio aqui vira falso negativo: briefing sem linha de clima passa
        # como "dia sem nada digno de nota" quando na verdade não se checou.
        # Mesma regra do DOU — fonte externa que falha é DITA.
        logger.warning("proactive: previsão do tempo falhou", exc_info=True)
        return [ProactiveFact(
            "clima", "clima_falhou", now_brt.date().isoformat(),
            f"⚠️ Não consegui checar a <b>previsão do tempo</b> agora "
            f"({type(exc).__name__}). NÃO assuma tempo firme — confira antes "
            "de sair.",
        )]
    linha = format_weather_line(w)
    if label:
        linha = f"✈️ {label.strip()}: {linha}"
    return [ProactiveFact("clima", "clima_hoje", "", linha)]


async def collect_moeda_viagem(user: User) -> list[ProactiveFact]:
    """Cotação da moeda local no briefing DURANTE a viagem (se configurada
    com 'moeda X'). Sem dedup (leitura fresca por dia)."""
    from bot.services.viagem import viagem_ativa
    moeda = getattr(user, "viagem_moeda", None)
    if not moeda or not viagem_ativa(user):
        return []
    try:
        from bot.services.cotacao import consultar_cotacao
        linha = await consultar_cotacao(moeda)
    except Exception as exc:
        # Falha ≠ silêncio: o usuário CONFIGUROU a moeda e conta com ela no
        # briefing. Sumir sem dizer nada (e sem ele saber que 'peso' era
        # inválido, p.ex.) esconde o defeito por semanas.
        logger.warning("proactive: cotação da moeda da viagem falhou", exc_info=True)
        return [ProactiveFact(
            "clima", "moeda_viagem_erro", "",
            f"💱 Não consegui a cotação de <b>{moeda}</b> ({type(exc).__name__}) — "
            "confira o nome da moeda com /viagem.",
        )]
    return [ProactiveFact("clima", "moeda_viagem", "", f"💱 {linha}")]


async def collect_transito(
    session: AsyncSession, user: User, now_brt: datetime,
) -> list[ProactiveFact]:
    """Trânsito casa → trabalho pro briefing matinal (dias úteis). Reusa o
    fetch do digest de trânsito. Sem dedup (leitura fresca a cada manhã).

    Falha e config faltando são DITAS, não engolidas (mesma correção que o
    collect_clima já recebeu — o trânsito ficou de fora dela): briefing sem
    a linha 🚗 era indistinguível de fim de semana, e o dono saía achando a
    rota normal quando o Maps nem tinha sido consultado."""
    if now_brt.weekday() > 4:  # fim de semana: sem trânsito pro trabalho
        return []
    if not (settings.home_coords and settings.work_coords and settings.google_maps_api_key):
        # Config incompleta é permanente: avisa UMA vez. Marcado AQUI (não no
        # pós-envio do run, que pula a categoria 'transito' de propósito —
        # a linha normal repete todo briefing).
        logger.warning("proactive: trânsito sem config — briefing sai sem a linha")
        if await already_notified(session, user.id, "transito_sem_config", ""):
            return []
        await mark_notified(session, user.id, "transito_sem_config", "")
        return [ProactiveFact(
            "transito", "transito_sem_config", "",
            "ℹ️ O briefing está saindo <b>sem a linha de trânsito</b>: faltam "
            "<code>HOME_COORDS</code>/<code>WORK_COORDS</code>/"
            "<code>GOOGLE_MAPS_API_KEY</code> no .env. Configurando, ela "
            "volta sozinha.",
        )]
    import httpx
    from bot.services.traffic import (
        USER_AGENT as TRAFFIC_USER_AGENT,
        fetch_traffic_with_alternative,
        format_traffic_briefing,
        parse_route_waypoints,
    )
    api_key = settings.google_maps_api_key.get_secret_value()
    try:
        async with httpx.AsyncClient(
            timeout=20.0, follow_redirects=True,
            headers={"User-Agent": TRAFFIC_USER_AGENT},
        ) as client:
            waypoints: list[str] = []
            if settings.route_google_maps_url:
                waypoints = await parse_route_waypoints(client, settings.route_google_maps_url)
            # Duas rotas comparadas (mesma leitura do /transito_agora), não só uma.
            pref, alt = await fetch_traffic_with_alternative(
                client, api_key, settings.home_coords, settings.work_coords,
                waypoints, maps_url=settings.route_google_maps_url or "",
            )
    except Exception as exc:
        # Silêncio aqui vira falso negativo: briefing sem a linha 🚗 passa
        # como "rota normal" quando na verdade não se checou. Mesma regra do
        # clima/DOU — fonte externa que falha é DITA.
        logger.warning("proactive: trânsito casa→trabalho falhou", exc_info=True)
        return [ProactiveFact(
            "transito", "transito_falhou", now_brt.date().isoformat(),
            f"⚠️ Não consegui checar o <b>trânsito casa → trabalho</b> agora "
            f"({type(exc).__name__}). NÃO assuma via livre — confira antes "
            "de sair.",
        )]
    txt = format_traffic_briefing(pref, alt)
    return [ProactiveFact("transito", "transito_trabalho", "", txt)]


async def collect_carteira(
    session: AsyncSession, user: User, now_brt: datetime, *, force: bool = False,
) -> list[ProactiveFact]:
    """Revisão da carteira (ações/FIIs/ETFs) na ÚLTIMA janela do dia: busca a
    cotação de mercado atual (brapi), atualiza o currentPrice no Firestore e
    monta valor investido vs valor de mercado por ativo. Tesouro fica fora
    (não tem cotação de bolsa). 1×/dia (deduplicado por data)."""
    last_hour = max(parse_proactive_hours(settings.proactive_hours))
    if not force and now_brt.hour != last_hour:
        return []
    try:
        from bot.services.financeiro import (
            atualizar_cotacoes_carteira,
            format_carteira_review,
            get_carteira_tickers,
        )
        from bot.services.quotes import QuotesError, fetch_quotes

        tickers = await get_carteira_tickers(session, user)
        if not tickers:
            return []
        try:
            prices = await fetch_quotes(tickers)
        except QuotesError as e:
            logger.warning("proactive: cotação indisponível (%s)", e)
            return []
        if not prices:
            return []
        assets = await atualizar_cotacoes_carteira(
            session, user, prices, today_iso=now_brt.date().isoformat(),
        )
        text = format_carteira_review(assets, prices)
        if not text:
            return []
    except Exception:
        logger.exception("proactive: revisão de carteira falhou p/ user %s", user.id)
        return []
    key = now_brt.date().isoformat()
    return [ProactiveFact("carteira", "carteira_review", key, text)]


# ──────────────────────── orquestrador ────────────────────────

_CAT_HEADER = {
    "clima": "🌦️ <b>Clima hoje</b>",
    "transito": "🚗 <b>Trânsito casa → trabalho</b>",
    "venc": "⏳ <b>Chegando</b>",
    "tarefas": "📋 <b>Tarefas abertas</b>",
    "mp": "📜 <b>Diário Oficial</b>",
    "nudge": "💡 <b>Hábitos</b>",
    "carteira": "📈 <b>Carteira hoje</b>",
}


def _compose(facts: list[ProactiveFact], *, briefing: bool) -> str:
    blocks: list[str] = []
    if briefing:
        blocks.append("☀️ <b>Bom dia! Resumo de hoje</b>")
    for cat in ("clima", "transito", "venc", "tarefas", "mp", "nudge", "carteira"):
        lines = [f.text for f in facts if f.category == cat]
        if not lines:
            continue
        blocks.append(_CAT_HEADER[cat] + "\n" + "\n".join(lines))
    return "\n\n".join(blocks)


async def _send(bot, chat_id: int, text: str, reply_markup=None) -> bool:
    """Envia o proativo em HTML (fallback texto puro), quebrando em blocos.

    Briefing com MPs + digest passa de 4096 chars com facilidade, e sem o
    chunk a mensagem falhava nas DUAS tentativas — o usuário simplesmente não
    recebia o briefing do dia. Teclado só no último bloco."""
    from bot.utils import chunk_text

    blocos = chunk_text(text, mode="html") or [""]
    ok = True
    for i, bloco in enumerate(blocos):
        kb = reply_markup if i == len(blocos) - 1 else None
        try:
            await bot.send_message(chat_id, bloco, parse_mode="HTML",
                                   disable_web_page_preview=True, reply_markup=kb)
        except Exception:
            logger.exception("proactive: HTML send failed; retrying plain for %d", chat_id)
            try:
                await bot.send_message(chat_id, bloco, parse_mode=None,
                                       disable_web_page_preview=True, reply_markup=kb)
            except Exception:
                logger.exception("proactive: failed to send to %d", chat_id)
                ok = False
    return ok


async def _redigir(user: User, deterministic: str) -> str:
    """Redação opcional via LLM (sem tools). Fallback ao texto determinístico."""
    if not settings.proactive_use_llm:
        return deterministic
    try:
        from bot.services.llm.factory import get_provider_for_user
        provider = get_provider_for_user(user)
        out = await provider.chat(
            [{"role": "user", "content": deterministic}],
            system=_PROACTIVE_SYSTEM, max_tokens=400,
        )
        return (out or "").strip() or deterministic
    except Exception:
        logger.exception("proactive: LLM redação falhou; usando texto determinístico")
        return deterministic


def run_key_da_janela(window: str, today: date, hour: int) -> str:
    """Chave da trava de janela. BRIEFING é POR DIA (sem hora): com o
    catch-up do scheduler (tentativas a cada hora até meio-dia quando o bot
    perdeu as 7h), a chave com hora deixaria o mesmo briefing rodar de novo
    a cada hora do catch-up. Janela regular continua por (dia, hora)."""
    if window == "briefing":
        return f"briefing:{today.isoformat()}"
    return f"{window}:{today.isoformat()}:{hour}"


async def run_for_user(
    bot, session: AsyncSession, user: User, now_brt: datetime, *,
    window: str, force: bool = False,
) -> bool:
    """Coleta fatos da janela, monta UMA mensagem e envia. Marca dedup só
    após envio OK. Retorna True se enviou."""
    briefing = window == "briefing"
    today = now_brt.date()
    # Datas do DOU SEMPRE em BRT: o Diário é de Brasília. `now_brt` aqui é o
    # relógio LOCAL do usuário (viagem conta) — em Tóquio (UTC+9) o "hoje"
    # local é o AMANHÃ de Brasília: as janelas checavam um dia sem edição,
    # enfileiravam pendência-fantasma e a MP do dia corrente só aparecia no
    # briefing seguinte. A JANELA dispara no fuso da viagem (correto); as
    # DATAS consultadas, não.
    hoje_dou = datetime.now(BRT).date()
    mp_dates = [hoje_dou - timedelta(days=1), hoje_dou] if briefing else [hoje_dou]

    # Trava de nível-janela: roda 1x por janela (briefing: por dia; regular:
    # por dia+hora — ver run_key_da_janela). Sem isso, como o tick é de ~20s
    # e a janela é minute<=1, rodaria ~5x — refazendo fetch de DOU/coletas à
    # toa. Marca já na entrada (mesmo que dê "sem fatos") pra os ticks
    # seguintes pularem. force (/proativo_agora) ignora a trava.
    if not force:
        run_key = run_key_da_janela(window, today, now_brt.hour)
        if await already_notified(session, user.id, "proactive_run", run_key):
            return False
        await mark_notified(session, user.id, "proactive_run", run_key)

    # Resumo do fim do dia = última janela proativa (mesma régua da carteira).
    last_hour = max(parse_proactive_hours(settings.proactive_hours))
    end_of_day = (not briefing) and (force or now_brt.hour == last_hour)

    facts: list[ProactiveFact] = []
    if briefing:
        facts += await collect_clima(session, user, now_brt)
        facts += await collect_transito(session, user, now_brt)
        facts += await collect_moeda_viagem(user)
    facts += await collect_vencimentos(session, user, now_brt, force=force)
    # Tarefas abertas no briefing matinal e no resumo do fim do dia.
    if briefing or end_of_day:
        facts += await collect_tarefas(session, user, now_brt)
    facts += await collect_mp(
        session, user, mp_dates, force=force, conferir=briefing or force,
    )
    facts += await collect_nudges(session, user, now_brt, force=force)
    if not briefing:
        facts += await collect_carteira(session, user, now_brt, force=force)

    # Fila de notas técnicas pendentes (pedidas com Inlabs fora): só AGENDA a
    # re-tentativa (task própria) — a geração leva minutos e não pode atrasar
    # a mensagem da janela nem o lembrete que vencer no meio.
    #
    # ANTES do `if not facts`: a fila NÃO depende de haver mensagem a enviar.
    # Ficava depois e só funcionava porque a própria fila gera a linha de
    # status `nota_fila`, que mantinha `facts` não-vazio — acoplamento, não
    # desenho: silenciar aquela linha um dia mataria a re-tentativa EM
    # SILÊNCIO, com o bot tendo prometido "te envio automaticamente".
    # Não dispara nota quando a checagem DESTE run não alcançou o Inlabs: o job
    # buscaria o mesmo Inlabs fora e falharia, e "gerando agora" seria mentira.
    # A linha de status já diz "aguardando o Inlabs voltar" (ver collect_mp).
    if user.dou_mp_subscribed and not getattr(user, "dou_fora_agora", False):
        try:
            disparadas = await _processar_notas_pendentes(bot, session, user)
        except Exception:
            logger.exception("proactive: fila de notas pendentes falhou p/ user %s", user.id)
            disparadas = []
        _marcar_geradas_agora(facts, disparadas)

    if not facts:
        logger.info("proactive: user %d window=%s sem fatos", user.id, window)
        return False

    text = await _redigir(user, _compose(facts, briefing=briefing))

    # Botão de nota técnica quando houver MP nos facts. Usa a data da MP
    # (não o `today` da execução), pra cobrir briefing que junta ontem+hoje.
    # Se houver MPs de mais de uma data, usa a mais recente — o usuário ainda
    # pode chamar /mp_dou_agora <data> pras outras. Passa os NÚMEROS detectados
    # nesta notificação (key = "numero/ano") pra nota cobrir só essas MPs — sem
    # isso o botão regerava todas as MPs do dia (ex.: 19h refazia as das 13h).
    reply_markup = None
    mp_facts = [f for f in facts if f.category == "mp" and f.date_iso]
    if mp_facts:
        from bot.handlers.dou_mp import nota_keyboard
        latest_date = max(f.date_iso for f in mp_facts)
        numeros = [f.key.split("/")[0] for f in mp_facts if f.date_iso == latest_date]
        reply_markup = nota_keyboard(latest_date, numeros)

    sent = await _send(bot, user.id, text, reply_markup=reply_markup)
    logger.info("proactive: user %d window=%s %d fatos enviado=%s", user.id, window, len(facts), sent)
    if sent:
        for f in facts:
            # Retroativa do DOU ENTREGUE → o dia sai da pendência. Isso NÃO é
            # dedup de aviso: é baixa de estado — o dia foi mesmo re-checado e
            # o resultado já foi entregue. Por isso vale inclusive no
            # /proativo_agora (force), que antes caía fora do bloco inteiro:
            # a pendência nunca era baixada, cada execução manual re-baixava
            # os ZIPs daquele dia (~100-200MB no Orange Pi) e a linha
            # "✅ retroativa concluída" se repetia pra sempre.
            # Se o envio falhar, a pendência fica e a retro repete na janela
            # seguinte — que é o comportamento desejado.
            if f.kind == "mp_retro":
                await unmark_notified(
                    session, user.id, "mp_pendente", f.key.removeprefix("retro:"),
                )
            # Daqui pra baixo é DEDUP de aviso, e o force pula de propósito:
            # execução de teste não pode silenciar a janela real.
            if force:
                continue
            # clima, trânsito e vencimentos não têm dedup: repetem a cada
            # janela (clima/trânsito = leitura fresca; vencimento = lembrar
            # até pagar).
            if f.category in ("clima", "transito", "venc", "tarefas"):
                continue
            # nota_fila é linha de STATUS: repete a cada janela até a entrega
            # dar baixa na pendência (sem dedup).
            if f.kind == "nota_fila":
                continue
            await mark_notified(session, user.id, f.kind, f.key)

    return sent


async def purge_old_notices(session: AsyncSession, days: int = 90) -> int:
    cut = datetime.now(timezone.utc) - timedelta(days=days)
    res = await session.execute(delete(ProactiveNotice).where(ProactiveNotice.sent_at < cut))
    await session.commit()
    return res.rowcount or 0
