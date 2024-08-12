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

# How to contribute to the AI Horde code

We are happy you have ideas to improve this service and we welcome all contributors.

Once you have an idea you want to work on, we suggest you first open a [feature request](https://github.com/orgs/Haidra-Org/projects/14) as the AI Horde has a lot of moving pieces in multiple components which might need to be adjusted, such as the AI Horde API, the reGen worker or the horde-engine library.

Our codebase makes use of multiple tools to keep our work standardized. We typically use [pre-commit](https://pre-commit.com/) to ensure that all linting and code checking is performed.

You can install all the dev tools we're using by using `requirements.dev.txt` and then the pre-commit hook

```bash
python -m pip install -r requirements.dev.txt
pre-commit install
```

This will ensure that the project will prevent you from commiting changes which will not pass our CI checks.

The tools we're using are the following

## black

To format all files as necessary, simply run:

```bash
black .
```

## ruff

To adjust all files as necessary, simply run:


```bash
ruff . --fix
```

Ruff might request extra manual fixes

## reuse

[Reuse](https://reuse.software/) ensures that all license and contribution info is maintained. You usually don't need to do anything about this unless you add a new file, or you make significant changes in an existing one.


### New file

If you add a new file, you need to ensure that it has the copyright and license information with a command like this:

```bash
reuse annotate  --copyright db0 --license="AGPL-3.0-or-later"  horde/my_new_code.py
```

Simply replace the copyright with your own username and optionally email.

The license to choose is based on the file.

* For normal code contributions, use `AGPL-3.0-or-later`
* For non-GenAI art and other such creative contributions, use `CC-BY-SA-4.0`
* For trivial files such as .gitignore and Generative AI art, use `CC0-1.0`

### Existing file

If you did some significant contributions to the code (i.e. not things like syntax, spelling. linting etc), then just add your own copyrights to an existing file. For example:

```bash
reuse annotate  --copyright db0 horde/existing_code.py
```

Simply replace the copyright with your own username and optionally email.

Note that the pre-commit will not complain if you forget to add your copyright notice to an existing file.

# Code of Conduct

We expect all contributors to follow the [Anarchist code of conduct](https://wiki.dbzer0.com/the-anarchist-code-of-conduct/). Do not drive away other contributors due to intended or unintended on bigotry.