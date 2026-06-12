"""Arquivos anexados no Telegram → ./workspace/uploads (owner-only).

A pasta fica DENTRO do workspace do agente de propósito: o que você anexa
hoje é insumo de /agente amanhã ("pega o uploads/X e ..."). Persiste no
host via volume ./workspace. Sem aiogram aqui — o handler (handlers/
upload.py) e a tool listar_arquivos (services/tools.py) reusam isto.
"""
from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path

from bot.config import settings

# Limite de DOWNLOAD da Bot API (getFile) — menor que o de envio (50 MB).
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
_MAX_LIST = 50


def uploads_dir() -> Path:
    d = Path(settings.agent_workspace) / "uploads"
    d.mkdir(parents=True, exist_ok=True)
    return d


def sanitize_name(name: str) -> str:
    """Só o basename, sem path traversal nem caracteres exóticos."""
    name = Path(name.replace("\\", "/")).name.strip().strip(".")
    name = re.sub(r"[^\w.\- ()\[\]]", "_", name)
    return name or "arquivo"


def unique_path(name: str) -> Path:
    """Caminho livre em uploads/ — colisão vira nome-1.ext, nome-2.ext…"""
    d = uploads_dir()
    p = d / name
    i = 1
    while p.exists():
        p = d / f"{p.stem.rstrip('-0123456789') or p.stem}-{i}{p.suffix}"
        i += 1
    return p


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n} B"  # inalcançável; agrada o type checker


def list_files() -> list[tuple[str, int, datetime]]:
    """(caminho relativo, bytes, mtime) — mais recentes primeiro."""
    d = uploads_dir()
    out = []
    for p in d.rglob("*"):
        if p.is_file():
            st = p.stat()
            out.append((
                str(p.relative_to(d)), st.st_size,
                datetime.fromtimestamp(st.st_mtime),
            ))
    out.sort(key=lambda t: t[2], reverse=True)
    return out


def format_listing() -> str:
    files = list_files()
    if not files:
        return "nenhum arquivo salvo (anexe um documento no chat pra guardar)"
    lines = [
        f"{name} — {human_size(size)} — {mt:%d/%m/%Y %H:%M}"
        for name, size, mt in files[:_MAX_LIST]
    ]
    total = sum(s for _, s, _ in files)
    head = f"{len(files)} arquivo(s), {human_size(total)} no total"
    if len(files) > _MAX_LIST:
        lines.append(f"… e mais {len(files) - _MAX_LIST}")
    return head + ":\n" + "\n".join(lines)


def resolve_file(name: str) -> Path | None:
    """Caminho de um arquivo existente em uploads/, ou None. Recusa
    qualquer caminho que escape da pasta."""
    d = uploads_dir().resolve()
    target = (d / name).resolve()
    if not str(target).startswith(str(d) + os.sep):
        return None
    return target if target.is_file() else None


def delete_file(name: str) -> bool:
    """Apaga um arquivo de uploads/. False = não existe."""
    target = resolve_file(name)
    if target is None:
        return False
    target.unlink()
    return True
