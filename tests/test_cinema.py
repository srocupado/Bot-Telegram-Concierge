"""Testes da lógica pura de cinema (agrupamento de sessões, sem rede)."""
from bot.services.cinema import _group_sessions


def _s(features, audio, time):
    return {"features": features, "audio": audio, "time": time}


def test_group_sessions_por_tecnologia_e_audio():
    # features: 7=2D, 6=3D ; audio: 20=Dublado, 30=Legendado
    sessoes = [
        _s([7], 20, "11:20:00"),
        _s([7], 20, "14:40:00"),
        _s([6], 20, "12:50:00"),
        _s([7], 30, "13:00:00"),
    ]
    linhas = _group_sessions(sessoes)
    assert "• 2D Dublado: 11h20, 14h40" in linhas
    assert "• 3D Dublado: 12h50" in linhas
    assert "• 2D Legendado: 13h00" in linhas


def test_group_sessions_ordena_horarios():
    sessoes = [_s([7], 20, "20:40:00"), _s([7], 20, "11:20:00")]
    linhas = _group_sessions(sessoes)
    assert linhas == ["• 2D Dublado: 11h20, 20h40"]  # ordenado, não na ordem de entrada


def test_group_sessions_vazio():
    assert _group_sessions([]) == []


def test_group_sessions_ignora_horario_vazio():
    sessoes = [_s([7], 20, ""), _s([7], 20, "15:00:00")]
    linhas = _group_sessions(sessoes)
    assert linhas == ["• 2D Dublado: 15h00"]
