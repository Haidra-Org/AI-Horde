<!--
SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# How to run AI Horde locally.

* Git clone this repository
* copy `.env_template` into `.env` and edit it according to its comments. The horde should start if you leave it unedited
* install python requirements with `python -m pip install -r requirements.txt --user`
* start server with `python server.py  -vvvvi --horde stable`
* You can now connect to http://localhost:7001
