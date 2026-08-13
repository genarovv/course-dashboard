"""Прогоны — только на бэкенде asyncio.

anyio начиная с 4.5 по умолчанию параметризует @pytest.mark.anyio всеми
доступными бэкендами: в окружении без trio это давало бы десятки ложных
[trio]-падений, а канон проекта — один бэкенд (см. счёт тестов в MAP.md).
params сохраняет привычные id вида test_x[asyncio].
"""

import pytest


@pytest.fixture(scope="module", params=["asyncio"])
def anyio_backend(request):
    return request.param
