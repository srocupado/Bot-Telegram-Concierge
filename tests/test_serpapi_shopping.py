"""search_shopping: `location=Brazil` é obrigatório no engine google_shopping.

Bug do dono (03/08/2026): "busca o preço do DJI Avata 2 Fly More Combo"
voltava sem preço — o SerpAPI respondia "Google hasn't returned any results
for this query" e o buscar_preco caía num fallback web inútil (páginas de
e-commerce exigem login). MEDIDO com curls no Orange Pi contra o SerpAPI
real: a MESMA query exata sem `location` → vazio; com `location=Brazil` →
37 ofertas. hl/gl sozinhos não bastam pro matching de query específica.
"""
from __future__ import annotations

import asyncio

from bot.services.travels.serpapi_client import SerpAPIClient


def test_search_shopping_manda_location_brazil(monkeypatch) -> None:
    client = SerpAPIClient("k")
    capturado: dict = {}

    async def _get(params):
        capturado.update(params)
        return {}

    monkeypatch.setattr(client, "_get", _get)

    async def _main():
        try:
            await client.search_shopping("DJI Avata 2 Fly More Combo")
        finally:
            await client.close()

    asyncio.run(_main())
    assert capturado["engine"] == "google_shopping"
    assert capturado["location"] == "Brazil", (
        "sem location o Shopping volta vazio pra query específica "
        "(medido: 0 → 37 ofertas na mesma busca)"
    )
    assert capturado["q"] == "DJI Avata 2 Fly More Combo"
    assert capturado["gl"] == "br" and capturado["hl"] == "pt-br"
