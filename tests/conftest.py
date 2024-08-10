# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import pathlib

import pytest
import requests


@pytest.fixture(scope="session")
def CIVERSION() -> str:
    return "0.1.1"


@pytest.fixture(scope="session")
def HORDE_URL() -> str:
    return "localhost:7001"


@pytest.fixture(scope="session")
def api_key() -> str:
    key_file = pathlib.Path(__file__).parent / "apikey.txt"
    if key_file.exists():
        return key_file.read_text().strip()

    raise ValueError("No api key file found")


@pytest.fixture(autouse=True, scope="session")
def increase_kudos(api_key: str, HORDE_URL: str, CIVERSION: str) -> None:
    headers = {"apikey": api_key, "Client-Agent": f"aihorde_ci_client:{CIVERSION}:(discord)db0#1625", "user_id": "1"}

    payload_set_to_mod = {
        "trusted": True,
        "moderator": True,
    }

    response_set_to_mod = requests.put(f"http://{HORDE_URL}/api/v2/users/1", json=payload_set_to_mod, headers=headers)

    assert response_set_to_mod.ok, response_set_to_mod.text

    payload_set_kudos = {
        "kudos": 10000,
    }

    response_kudos = requests.put(f"http://{HORDE_URL}/api/v2/users/1", json=payload_set_kudos, headers=headers)

    assert response_kudos.ok, response_kudos.text
