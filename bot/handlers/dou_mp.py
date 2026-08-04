"""Comandos do monitor de MPs no Diário Oficial (Inlabs/DOU).

/mp_dou_on  /mp_dou_off  — assina/desassina o digest diário (18h BRT).
/mp_dou_agora [data] — força a busca de hoje (ou data: DD/MM/AAAA,
DD-MM-AA, AAAA-MM-DD…).
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db.models import User
from bot.services.dou_monitor import DouError, chave_job_nota, deliver_to_user

logger = logging.getLogger(__name__)
router = Router(name="dou_mp")

# Aliases de modelo Claude pra nota técnica (/dou_provider anthropic <alias>).
# sonnet é o recomendado: qualidade alta a ~metade do custo do opus.
_ANTHROPIC_VARIANTS = {
    "sonnet": "claude-sonnet-4-6",
    "opus": "claude-opus-4-8",
}


async def _list_claude_models() -> list[tuple[str, str]]:
    """[(id, display_name)] dos modelos Claude via Models API — DINÂMICO, então
    modelos novos (ex.: claude-sonnet-5) aparecem sozinhos sem mexer no código.
    [] se faltar ANTHROPIC_API_KEY ou a API falhar."""
    if not settings.anthropic_api_key:
        return []
    try:
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        out: list[tuple[str, str]] = []
        async for m in client.models.list():
            mid = getattr(m, "id", "") or ""
            if mid.startswith("claude-"):
                out.append((mid, getattr(m, "display_name", "") or mid))
        return out
    except Exception:
        logger.exception("dou: falha ao listar modelos Anthropic")
        return []


def nota_keyboard(date_iso: str, numeros: list[str] | None = None) -> InlineKeyboardMarkup:
    """Botões Sim/Não pra oferecer a nota técnica das MPs de uma data.

    Se `numeros` for dado, a nota cobre SOMENTE essas MPs (os números detectados
    naquela notificação) — evita regerar todas as MPs do dia quando uma janela
    posterior avisa de uma MP nova. callback_data tem teto de 64 bytes; se a
    lista não couber, cai pro modo data (todas as MPs do dia)."""
    cb = f"doump:y:{date_iso}"
    if numeros:
        candidate = f"{cb}:{','.join(numeros)}"
        if len(candidate.encode()) <= 64:
            cb = candidate
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📄 Sim, gerar nota", callback_data=cb),
        InlineKeyboardButton(text="Não", callback_data="doump:n"),
    ]])


@router.callback_query(F.data == "doump:n")
async def cb_nota_nao(query: CallbackQuery, user: User) -> None:
    await query.answer("Ok, sem nota.")
    try:
        await query.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


async def _rodar_nota(
    bot, user_id: int, target: date, only_numeros: list[str] | None,
) -> None:
    """Pipeline da nota em background, com sessão PRÓPRIA.

    Sai do handler de propósito: o pipeline leva minutos (download do DOU +
    pesquisa + redação + DOCX, uma MP por vez) e, rodando dentro do handler,
    segurava a sessão de banco do update por todo esse tempo. Aqui o usuário
    recebe a confirmação na hora e o trabalho segue sozinho.

    Os erros são reportados AQUI (o handler já respondeu): DouError vira fila
    de nota pendente — o proativo re-tenta e entrega quando o Inlabs voltar —
    e o resto vira aviso explícito. Nunca silêncio."""
    from bot.db.session import SessionLocal

    async with SessionLocal() as session:
        # Recarrega o usuário NA SESSÃO DO JOB: o objeto do handler pertence a
        # uma sessão que já fechou, e objeto ORM de sessão morta estoura em
        # DetachedInstanceError no primeiro atributo não carregado.
        user = await session.get(User, user_id)
        if user is None or not user.is_authorized:
            return
        try:
            n, falhas, motivo = await deliver_to_user(
                bot, session, user, target, force=True, only_numeros=only_numeros,
            )
        except DouError as e:
            from bot.services.proactive import already_notified, mark_notified
            key = f"{target.isoformat()}:{','.join(only_numeros) if only_numeros else 'all'}"
            if not await already_notified(session, user.id, "nota_pendente", key):
                await mark_notified(session, user.id, "nota_pendente", key)
            await bot.send_message(
                user.id,
                f"⚠️ {e}\n📄 Deixei a nota na fila: assim que o Inlabs voltar, "
                "gero e te envio automaticamente (sem precisar pedir de novo).",
                parse_mode=None,
            )
            return
        except Exception:
            logger.exception("nota em background falhou (%s)", target)
            await bot.send_message(
                user.id, "⚠️ Erro ao gerar a nota técnica.", parse_mode=None,
            )
            return
        # Baixa manual: checagem conclusiva de dia fechado tira o dia da fila
        # retroativa (senão a próxima janela re-baixava o DOU só pra confirmar
        # o que o dono acabou de ver). Subset (only_numeros) não dá baixa: o
        # botão do proativo entrega PARTE do dia — a verificação segue na fila.
        # Falha aqui não pode derrubar a entrega já feita: vira log e a
        # pendência fica (lado seguro).
        baixado = False
        if only_numeros is None:
            from bot.services.proactive import baixa_checagem_manual
            try:
                baixado = await baixa_checagem_manual(
                    session, user, target, n, falhas, motivo,
                )
            except Exception:
                logger.exception("baixa manual das pendências falhou (%s)", target)
        if n == 0:
            from bot.services.dou_monitor import texto_sem_mp
            texto = texto_sem_mp(motivo, target)
            if baixado:
                texto += " Dei baixa: o dia sai da fila de re-checagem."
            await bot.send_message(user.id, texto, parse_mode="HTML")


# A chave vive no serviço porque o proativo também dispara esse job: os dois
# caminhos PRECISAM concordar na chave pra dedup funcionar entre eles.
_chave_nota = chave_job_nota


@router.callback_query(F.data.startswith("doump:y:"))
async def cb_nota_sim(query: CallbackQuery, user: User, session: AsyncSession) -> None:
    """callback_data = 'doump:y:<AAAA-MM-DD>' (todas as MPs do dia) ou
    'doump:y:<AAAA-MM-DD>:<num1,num2,...>' (só essas MPs — vindo do proativo,
    que avisa de um subconjunto e não deve regerar o dia todo)."""
    if not user.is_authorized:
        await query.answer()
        return
    try:
        payload = query.data.split(":", 2)[2]  # "<data>" ou "<data>:<nums>"
        date_part, _, nums_part = payload.partition(":")
        target = date.fromisoformat(date_part)
        only_numeros = [n for n in nums_part.split(",") if n] or None
    except (ValueError, IndexError):
        await query.answer("⚠️ data inválida", show_alert=True)
        return
    await query.answer("Gerando a nota técnica…")
    try:
        await query.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    # Em BACKGROUND: o pipeline leva minutos e não pode segurar este handler
    # (nem a sessão de banco dele). Ver bot/services/jobs.py.
    from bot.services import jobs
    chave = _chave_nota(user.id, target)
    bot_ = query.bot
    if not jobs.spawn(chave, lambda: _rodar_nota(bot_, user.id, target, only_numeros)):
        await query.message.answer(
            "📄 Já estou gerando a nota dessa data — te mando assim que sair.",
            parse_mode=None,
        )
        return
    await query.message.answer(
        "📄 Gerando a nota técnica… leva alguns minutos (pesquisa + redação + "
        "DOCX). Pode seguir usando o bot normalmente; te aviso quando sair.",
        parse_mode=None,
    )


@router.message(Command("dou_provider"))
async def cmd_dou_provider(
    message: Message, command: CommandObject, user: User, session: AsyncSession
) -> None:
    """Escolhe o motor da nota técnica por usuário (vale na agendada e na
    on-demand). Sem args mostra o atual.

    /dou_provider                   → mostra atual
    /dou_provider anthropic         → Claude, modelo do .env (ANTHROPIC_MODEL)
    /dou_provider anthropic <alias> → Claude + modelo (sonnet | opus)
    /dou_provider gemini            → gemini, modelo do .env
    /dou_provider gemini <alias>    → gemini + modelo específico
    /dou_provider <alias>           → atalho: gemini-* assume gemini;
                                      sonnet/opus assumem anthropic
    /dou_provider padrao            → volta ao .env
    """
    from bot.handlers.provider import _GEMINI_VARIANTS

    if not user.is_authorized:
        return
    tokens = (command.args or "").strip().lower().split()

    if not tokens:
        prov = user.dou_mp_provider or settings.dou_mp_provider
        if prov == "gemini":
            label = f"gemini ({user.dou_mp_model or settings.dou_mp_gemini_model})"
        else:
            label = f"anthropic ({user.dou_mp_model or settings.anthropic_model})"
        gem_aliases = ", ".join(sorted(set(_GEMINI_VARIANTS)))
        ant_aliases = ", ".join(sorted(_ANTHROPIC_VARIANTS))
        await message.answer(
            f"Motor da nota técnica: <b>{label}</b>\n"
            f"Fallback gemini (fixo no .env): <code>{settings.dou_mp_gemini_model_fallback}</code>\n\n"
            "<b>Comandos:</b>\n"
            "<code>/dou_provider anthropic [alias]</code> · Claude (web_search)\n"
            "<code>/dou_provider gemini [alias]</code> · Gemini\n"
            "<code>/dou_provider &lt;alias&gt;</code> · atalho (infere o provider)\n"
            "<code>/dou_provider modelos</code> · lista os modelos Claude da API\n"
            "<code>/dou_provider anthropic &lt;id&gt;</code> · qualquer id (ex.: claude-sonnet-5)\n"
            "<code>/dou_provider padrao</code> · volta ao .env\n\n"
            f"<b>Aliases Gemini:</b> {gem_aliases}\n"
            f"<b>Aliases Claude:</b> {ant_aliases} <i>(ou use o id completo — veja /dou_provider modelos)</i>",
            parse_mode="HTML",
        )
        return

    if tokens[0] in ("modelos", "modelo", "listar", "models", "list"):
        modelos = await _list_claude_models()
        if not modelos:
            await message.answer(
                "Não consegui listar os modelos Claude agora "
                "(sem ANTHROPIC_API_KEY ou API fora).", parse_mode=None,
            )
            return
        atual = user.dou_mp_model or settings.anthropic_model
        linhas = [
            f"• <code>{mid}</code> — {nome}" + (" ⬅️ atual" if mid == atual else "")
            for mid, nome in modelos
        ]
        await message.answer(
            "<b>Modelos Claude disponíveis</b> (direto da API):\n" + "\n".join(linhas)
            + "\n\nPra usar na nota: <code>/dou_provider anthropic &lt;id&gt;</code>",
            parse_mode="HTML",
        )
        return

    arg = tokens[0]
    variant = tokens[1] if len(tokens) > 1 else None

    if arg in ("padrao", "padrão", "auto", "limpar", "none"):
        user.dou_mp_provider = None
        user.dou_mp_model = None
        await session.commit()
        await message.answer(
            f"✅ Nota técnica volta ao default do .env "
            f"(<b>{settings.dou_mp_provider}</b> · {settings.dou_mp_gemini_model}).",
            parse_mode="HTML",
        )
        return

    if arg == "anthropic":
        if variant is None:
            user.dou_mp_model = None  # volta ao ANTHROPIC_MODEL do .env
        elif variant in _ANTHROPIC_VARIANTS:
            user.dou_mp_model = _ANTHROPIC_VARIANTS[variant]
        elif variant.startswith("claude-"):
            # id completo (ex.: claude-sonnet-5) — valida na Models API quando
            # possível; se a lista vier vazia (sem chave), aceita e a API valida no uso.
            ids = {m[0] for m in await _list_claude_models()}
            if ids and variant not in ids:
                await message.answer(
                    f"Modelo <code>{variant}</code> não está na lista da API. "
                    "Veja <code>/dou_provider modelos</code>.", parse_mode="HTML",
                )
                return
            user.dou_mp_model = variant
        else:
            opts = ", ".join(sorted(_ANTHROPIC_VARIANTS))
            await message.answer(
                f"Use um alias ({opts}), um id <code>claude-…</code>, ou "
                "<code>/dou_provider modelos</code> pra listar.", parse_mode="HTML",
            )
            return
        user.dou_mp_provider = "anthropic"
        await session.commit()
        await message.answer(
            f"✅ Nota técnica via <b>anthropic ({user.dou_mp_model or settings.anthropic_model})</b> "
            "(Claude + web_search).",
            parse_mode="HTML",
        )
        return

    if arg == "gemini":
        if variant is None:
            user.dou_mp_model = None  # volta ao DOU_MP_GEMINI_MODEL do .env
        elif variant in _GEMINI_VARIANTS:
            user.dou_mp_model = _GEMINI_VARIANTS[variant]
        else:
            opts = ", ".join(sorted(set(_GEMINI_VARIANTS)))
            await message.answer(f"Alias Gemini inválido. Opções: {opts}", parse_mode=None)
            return
        user.dou_mp_provider = "gemini"
        await session.commit()
        await message.answer(
            f"✅ Nota técnica via <b>gemini ({user.dou_mp_model or settings.dou_mp_gemini_model})</b>.",
            parse_mode="HTML",
        )
        return

    # Atalhos de uma palavra: sonnet/opus → anthropic; gemini-* → gemini.
    if arg in _ANTHROPIC_VARIANTS:
        user.dou_mp_provider = "anthropic"
        user.dou_mp_model = _ANTHROPIC_VARIANTS[arg]
        await session.commit()
        await message.answer(
            f"✅ Nota técnica via <b>anthropic ({user.dou_mp_model})</b> (Claude + web_search).",
            parse_mode="HTML",
        )
        return

    if arg in _GEMINI_VARIANTS:
        user.dou_mp_provider = "gemini"
        user.dou_mp_model = _GEMINI_VARIANTS[arg]
        await session.commit()
        await message.answer(
            f"✅ Nota técnica via <b>gemini ({user.dou_mp_model})</b>.",
            parse_mode="HTML",
        )
        return

    await message.answer(
        "Opção inválida. Use: /dou_provider anthropic [sonnet|opus] | "
        "gemini [alias] | padrao",
        parse_mode=None,
    )


@router.message(Command("mp_dou_on"))
async def cmd_on(message: Message, user: User, session: AsyncSession) -> None:
    if not user.is_authorized:
        return
    from bot.services.proactive import _fmt_hora_janela, parse_proactive_hours

    user.dou_mp_subscribed = True
    await session.commit()
    # Horas REAIS das janelas (o antigo "todo dia às 18h" vinha do
    # DOU_MP_HOUR, config morta de antes do proativo — mentia pro dono).
    horas = sorted(parse_proactive_hours(settings.proactive_hours))
    janelas = ", ".join(_fmt_hora_janela(h) for h in horas)
    aviso = ""
    if not settings.proactive_enabled:
        # Dependência SILENCIOSA: o monitor roda dentro do agente proativo.
        # Assinar com ele desligado era prometer avisos que nunca viriam.
        aviso = (
            "\n\n⚠️ ATENÇÃO: o agente proativo está DESLIGADO "
            "(PROACTIVE_ENABLED=false no .env) — é ele que roda este monitor. "
            "Sem ligá-lo, as MPs NÃO chegam automaticamente."
        )
    await message.answer(
        f"✅ Monitor de MPs no DOU ativado. As checagens rodam nas janelas "
        f"do proativo ({janelas}), com abertura no briefing e fechamento do "
        f"dia na última janela.{aviso}\n"
        "Use /mp_dou_agora pra checar agora.",
        parse_mode=None,
    )


@router.message(Command("mp_dou_off"))
async def cmd_off(message: Message, user: User, session: AsyncSession) -> None:
    if not user.is_authorized:
        return
    user.dou_mp_subscribed = False
    await session.commit()
    await message.answer("🔕 Monitor de MPs no DOU desativado.", parse_mode=None)


def _fmt_alvo(nums: str) -> str:
    """'all'→'todas as MPs'; '1382'→'MP 1382'; '1382,1383'→'MPs 1382, 1383'."""
    if not nums or nums == "all":
        return "todas as MPs"
    partes = [n for n in nums.split(",") if n]
    if not partes:
        return "todas as MPs"
    return ("MP " if len(partes) == 1 else "MPs ") + ", ".join(partes)


def _fmt_fila_mp(fila: dict) -> str:
    """Monta o texto verbatim do /mp_em_fila a partir do snapshot da fila.
    Função pura (sem I/O) pra dar pra testar o texto sem banco."""
    notas = fila.get("notas") or []
    dias = fila.get("dias") or []
    manut = bool(fila.get("manutencao"))

    if not notas and not dias:
        base = ("✅ <b>Fila do DOU vazia</b> — nada pendente de checagem nem "
                "de nota.")
        if manut:
            base += ("\n\n⚠️ <i>O Inlabs está em manutenção agora, mas não há "
                     "nada represado.</i>")
        return base

    linhas = ["📥 <b>Fila do monitor de MP</b>"]
    # Só afirma "Inlabs" quando há causa APURADA (manutenção declarada). Sem
    # isso, dizer "quando o Inlabs voltar" mentia com o Inlabs online — a
    # pendência costuma ser dia em aberto/checagem, não Inlabs fora.
    if manut:
        linhas.append("⚠️ <i>Inlabs em manutenção agora — a fila drena quando "
                      "ele voltar.</i>")
    if notas:
        linhas.append("\n📄 <b>Notas técnicas na fila</b>:")
        for d, nums in notas:
            quando = d.strftime("%d/%m/%Y") if d else "data ?"
            linhas.append(f"• {_fmt_alvo(nums)} de {quando}")
    if dias:
        ultima_ok = fila.get("ultima_ok") or {}
        abertos = set(fila.get("abertos") or ())
        janelas = list(fila.get("janelas_hoje") or ())
        linhas.append("\n🔎 <b>Dias a verificar</b> (checo sozinho):")
        for d, restantes in dias:
            # Dia ABERTO: o desfecho esperado é HOJE (janelas restantes; a
            # extra das 19h pode resolver) ou no briefing de amanhã, quando o
            # dia fecha (6h) — NÃO "14 dias". O teto de desistência só
            # interessa (e só aparece) quando o dia está preso por falha.
            if d in abertos:
                if janelas:
                    from bot.services.proactive import _fmt_hora_janela
                    quando = " e às ".join(_fmt_hora_janela(h) for h in janelas)
                    estado = (f"re-checo hoje às {quando}; o desfecho sai "
                              "até o briefing de amanhã")
                else:
                    estado = ("fecho no briefing de amanhã (o dia encerra "
                              "de madrugada)")
            else:
                estado = ("re-checando a cada janela; desisto (com aviso) "
                          f"em {restantes} dia(s)")
            linha = f"• {d.strftime('%d/%m/%Y')} — {estado}"
            # Contexto da última checagem COMPLETA (quando houver): sem ele,
            # a linha logo após um "nenhuma MP" soava contraditória — como
            # se NADA daquele dia tivesse sido visto.
            ok = ultima_ok.get(d)
            if ok:
                quando_ok, n_mps = ok
                ate_entao = (f"{n_mps} MP(s) até então" if n_mps
                             else "sem MP até então")
                linha += f" · já checado {quando_ok.strftime('%d/%m %H:%M')} ({ate_entao})"
            linhas.append(linha)
    linhas.append("\nProcesso sozinho e te aviso o resultado — sem precisar "
                  "pedir de novo. Pra forçar uma data: "
                  "<code>/mp_dou_agora DD/MM/AAAA</code>.")
    return "\n".join(linhas)


@router.message(Command("mp_em_fila", "mp_fila"))
async def cmd_em_fila(message: Message, user: User, session: AsyncSession) -> None:
    """Mostra o que está na fila do monitor de MP: notas técnicas com número
    conhecido aguardando geração e dias que ainda serão verificados/re-checados.
    Read-only — não altera a fila."""
    if not user.is_authorized:
        return
    from bot.services.proactive import listar_fila_mp
    hoje = datetime.now(ZoneInfo(user.timezone)).date()
    fila = await listar_fila_mp(session, user.id, hoje)
    await message.answer(_fmt_fila_mp(fila), parse_mode="HTML")


def _parse_data_arg(arg: str, hoje: date) -> date | None:
    """Data do /mp_dou_agora: AAAA-MM-DD, DD/MM/AAAA, DD/MM/AA, DD-MM-AAAA,
    DD-MM-AA ou DD/MM (ano atual). SEM rolagem pro futuro — DOU é hoje/passado."""
    try:
        return date.fromisoformat(arg)
    except ValueError:
        pass
    m = re.fullmatch(r"(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?", arg)
    if not m:
        return None
    d_, mo = int(m[1]), int(m[2])
    yr = int(m[3]) if m[3] else hoje.year
    if yr < 100:
        yr += 2000
    try:
        return date(yr, mo, d_)
    except ValueError:
        return None


@router.message(Command("mp_dou_agora"))
async def cmd_agora(
    message: Message, command: CommandObject, user: User, session: AsyncSession,
) -> None:
    if not user.is_authorized:
        return
    arg = (command.args or "").strip()
    hoje = datetime.now(ZoneInfo(user.timezone)).date()
    if arg:
        target = _parse_data_arg(arg, hoje)
        if target is None:
            await message.answer(
                "Data inválida. Use DD/MM/AAAA (ex: 16/07/2026), DD-MM-AA ou AAAA-MM-DD.",
                parse_mode=None,
            )
            return
    else:
        target = hoje

    # Mesmo tratamento do botão: o pipeline sai do handler (ver _rodar_nota).
    from bot.services import jobs
    chave = _chave_nota(user.id, target)
    bot_ = message.bot
    if not jobs.spawn(chave, lambda: _rodar_nota(bot_, user.id, target, None)):
        await message.answer(
            f"📄 Já estou processando o DOU de {target.strftime('%d/%m/%Y')} — "
            "te mando assim que sair.", parse_mode=None,
        )
        return
    await message.answer(
        f"🔎 Buscando MPs publicadas no DOU em {target.strftime('%d/%m/%Y')}… "
        "se houver, a nota técnica leva alguns minutos e vem em seguida. "
        "Pode seguir usando o bot normalmente.",
        parse_mode=None,
    )
