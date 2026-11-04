<!--
SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
SPDX-FileCopyrightText: 2024 Tazlin <tazlin.on.github@gmail.com>

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# AI-Horde: Community-Powered AI Generation

[Generate images](https://aihorde.net/generate/image) · [Apps and integrations](https://aihorde.net/guis) · [Image guide](https://aihorde.net/docs/image) · [REST API](https://aihorde.net/api/) · [Service documentation](https://haidra-org.github.io/AI-Horde/) · [Discord](https://discord.gg/3DxrhksKzn)

> **What is it?** You can make images and text with open models for free, without an expensive computer, and no account or setup is needed. The Horde has no ads and does not sell your data. It is run by a non-profit and paid for by donations.
>
> **How do I start?**
>
> - **Images**: open the [image generator](https://aihorde.net/generate/image) in your browser, type a prompt, optionally select a style, and click generate. There are guided tutorials as well to get started.
> - **Text and chat**: use [KoboldAI Lite](https://lite.koboldai.net) in your browser, or another app from the [list of integrations](https://aihorde.net/guis).
> - **Faster results**: [register](https://aihorde.net/register) for a free API key and enter it in any app. Registered users get better queue priority than anonymous ones. [Contribute your GPU (Become a worker)](#contribute-your-gpu-become-a-worker) to earn kudos. The kudos you earn is equal to the amount of work your GPU contributes. You can always redeem them for as many requests as you generated, allowing you to "bank" compute.
> - **Your own software**: use the [REST API](https://aihorde.net/api/) or the [OpenAI-compatible endpoint](#openai-compatible-chat-completions).

The [AI Horde](https://github.com/Haidra-Org/haidra-assets/blob/main/docs/definitions.md#ai-horde) is a free, community-run service for [AI image generation](https://github.com/Haidra-Org/haidra-assets/blob/main/docs/definitions.md#image-generation) and [text generation](https://github.com/Haidra-Org/haidra-assets/blob/main/docs/definitions.md#text2text). Volunteers run [workers](https://github.com/Haidra-Org/haidra-assets/blob/main/docs/definitions.md#worker) on their own GPUs, and the Horde sends each [request](https://github.com/Haidra-Org/haidra-assets/blob/main/docs/definitions.md#request) to a worker that can serve it. The idea is the same as Folding@home or SETI@home: people share spare compute so that others can use it.

Anyone can use the AI Horde for free, even [anonymously](https://github.com/Haidra-Org/haidra-assets/blob/main/docs/definitions.md#anonymous). The Horde uses only open-source models and software, and this repository is the server that runs it. Our [mission page](https://aihorde.net/mission) explains why the project exists and what it commits to, and the [motivations document](https://github.com/Haidra-Org/haidra-assets/blob/main/docs/why.md) goes into more detail.

Priority in the queue is decided by [kudos](https://github.com/Haidra-Org/haidra-assets/blob/main/docs/kudos.md). Workers earn kudos for the jobs they complete and can spend them to move their own requests forward, or leave them unspent. Kudos cannot be bought or sold. Trading them is against the [Terms of Service](https://aihorde.net/terms). Users with more kudos get faster service, but everyone gets served, and kudos do not expire.

## Funding and Sponsors

The AI Horde is run by [Haidra](https://haidra.net), a volunteer-run, registered non-profit in Luxembourg. It is paid for entirely by donations. Volunteers donate the GPU time that does the generation, and financial donations pay for the servers, storage and bandwidth that connect everything. If the Horde is useful to you, please consider [supporting Haidra](https://haidra.net/donate/) or [running a worker](#contribute-your-gpu-become-a-worker).

[![NLnet logo](assets/logo_nlnet.svg)](https://nlnet.nl/project/AI-Horde/)

The AI Horde has received funding from [NLnet](https://nlnet.nl/project/AI-Horde/) through the NGI0 Core fund. See all [sponsors and credits](https://aihorde.net/sponsors).

## Table of Contents

- [AI-Horde: Community-Powered AI Generation](#ai-horde-community-powered-ai-generation)
    - [Funding and Sponsors](#funding-and-sponsors)
    - [Table of Contents](#table-of-contents)
    - [Technical Introduction](#technical-introduction)
        - [Features](#features)
        - [Public Service](#public-service)
        - [OpenAI-Compatible Chat Completions](#openai-compatible-chat-completions)
        - [Private Deployment](#private-deployment)
        - [Technologies Used](#technologies-used)
        - [Documentation](#documentation)
    - [Getting Started with AI Horde](#getting-started-with-ai-horde)
        - [OAuth2 Registered Account (Recommended)](#oauth2-registered-account-recommended)
        - [Pseudonymous Account](#pseudonymous-account)
        - [Anonymous Usage](#anonymous-usage)
        - [Why Register?](#why-register)
    - [Contribute your GPU (Become a worker)](#contribute-your-gpu-become-a-worker)
        - [Image](#image)
        - [Text](#text)
    - [Integrate with the AI-Horde](#integrate-with-the-ai-horde)
    - [Contributing to This Repository](#contributing-to-this-repository)
    - [Community](#community)
    - [License](#license)

## Technical Introduction

The AI Horde is a distributed inference service. The server queues requests, matches them to volunteer workers, collects the results and keeps the kudos accounts. Workers can join and leave at any time, and the Horde routes work to whichever workers are online and able to serve it.

### Features

- **Image generation**: text-to-image, img2img, inpainting, ControlNet, LoRAs, textual inversions and post-processing, across Stable Diffusion, SDXL, Flux and other open model families.
- **Alchemy**: image interrogation, captioning, upscaling and other post-processing on existing images.
- **Text generation**: prompt completion through the native API and chat completions through the [OpenAI-compatible endpoint](#openai-compatible-chat-completions), with streaming, tool calls, structured output, reasoning and image inputs where the worker supports them.
- **Styles**: shareable generation presets with a published parameter policy. Frontends can apply them without reimplementing the rules.
- **Kudos accounting**: an event-sourced ledger with reservations, so every charge and payment is recorded and can be audited.
- **Moderation tools**: content filters, evidence records and review queues for the volunteer moderators.

### Public Service

The [public service](https://aihorde.net) lets people without powerful GPUs use open models through the compute shared by the community. You can generate images directly on the website at [aihorde.net/generate/image](https://aihorde.net/generate/image), with no account required. The [image generation guide](https://aihorde.net/docs/image) walks through a first image and explains each setting. The [model reference](https://reference.aihorde.net) lists the models known to the Horde by category, with search.

Many apps, bots, games and plugins are built on the Horde. The [list of integrations](https://aihorde.net/guis) covers frontends for images and text, Discord and fediverse bots, editor plugins and developer tools such as the [Python SDK](https://github.com/Haidra-Org/horde-sdk).

Third-party websites, games and apps can use the [REST API](https://aihorde.net/api/) directly. Live status is on the [status page](https://aihorde.net/status), and performance statistics are on [our Grafana instance](https://grafana.aihorde.net/d/jSb16YLVk/performance?orgId=1).

### OpenAI-Compatible Chat Completions

The Horde has an OpenAI-compatible endpoint at `https://oai.aihorde.net/v1`. It supports `POST /v1/chat/completions` (streamed and non-streamed) and `GET /v1/models`, so most OpenAI client libraries and chat frontends work with it after changing the base URL. Send a Horde API key as the bearer token, or use the anonymous key `0000000000`.

```bash
curl https://oai.aihorde.net/v1/chat/completions \
  -H "Authorization: Bearer 0000000000" \
  -H "Content-Type: application/json" \
  -d '{"model": "<a model id from /v1/models>", "messages": [{"role": "user", "content": "Hello"}]}'
```

The [interactive API documentation](https://oai.aihorde.net/docs) lists the supported request keys and the differences from OpenAI, and the [chat front reference](https://haidra-org.github.io/AI-Horde/reference/chat_front/) covers them in full. The endpoint runs as its own process (`python -m horde.oai`) in front of the native chat API. To get started, see [your first chat completion](https://haidra-org.github.io/AI-Horde/tutorial/first_chat_completion/) and [send chat completions](https://haidra-org.github.io/AI-Horde/how-to/send_chat_completions/). Worker operators can read [serve your first chat job](https://haidra-org.github.io/AI-Horde/tutorial/first_chat_worker/).

### Private Deployment

The Horde server can also be deployed privately, for a team or an organization that wants to pool its own GPUs. Container images are published to the GitHub Container Registry, and the [Docker readme](README_docker.md) describes a full deployment with Docker Compose. A private horde does not share workers with the public one, so it needs its own.

### Technologies Used

- **Python** 3.12 or 3.13, with dependencies managed by [uv](https://docs.astral.sh/uv/)
- **Flask** and **Flask-RESTX** for the native REST API, served by **Waitress**
- **FastAPI** and **Uvicorn** for the OpenAI-compatible chat endpoint
- **PostgreSQL** for the main database and the kudos ledger
- **Redis** for caching, rate limits, locks and chat streams
- **S3-compatible object storage** (Cloudflare R2 on the public service) for images and large payloads
- **Docker** images and a Docker Compose file for the full stack
- **OpenTelemetry** instrumentation for tracing and metrics

### Documentation

- [Service documentation](https://haidra-org.github.io/AI-Horde/) (published from [docs/](docs/README.md)): tutorials, how-to guides, reference and explanation pages for developers, integrators and operators of this server, and the [architecture decision records](https://haidra-org.github.io/AI-Horde/decisions/).
- [Horde concepts](https://github.com/Haidra-Org/haidra-assets/tree/main/docs): what the Horde is, workers, the [request and job lifecycle](https://github.com/Haidra-Org/haidra-assets/blob/main/docs/workers.md) and [kudos](https://github.com/Haidra-Org/haidra-assets/blob/main/docs/kudos.md).
- [Image generation guide](https://aihorde.net/docs/image) on the website, for people making images.
- [FAQ](https://aihorde.net/faq) on the website.
- [Return codes](README_return_codes.md) for API errors.

## Getting Started with AI Horde

You can use any service built on the AI Horde with a registered account or anonymously. To try it right away, [generate an image on the website](https://aihorde.net/generate/image) or pick an app from the [list of integrations](https://aihorde.net/guis).

### OAuth2 Registered Account (Recommended)

> Note: The only information we store from your OAuth2 account is its unique ID, and we do not use it for any other purpose. See our [privacy policy](https://aihorde.net/privacy) for details.

- Visit [AI Horde Registration](https://aihorde.net/register).
- Log in with one of the supported OAuth2 services.
- Choose your username and receive your API key.
- Benefits:
  - Higher starting priority in the generation queues
  - You can change your username or reset your API key
  - Your kudos balance is kept and tracked
  - You can recover access through the OAuth2 service
  - Minimum kudos balance of 25

### Pseudonymous Account

- Visit [AI Horde Registration](https://aihorde.net/register).
- This is the default when you do not log in with an OAuth2 service (Google, GitHub, Discord, etc).
- **Important**: if you lose your API key, the account cannot be recovered.
- You cannot change your username or reset your API key.
- You still earn and keep kudos.
- You still get better priority than anonymous users.

### Anonymous Usage

- Use the API key `0000000000`.
- Lowest priority in the generation queues.
- No kudos tracking.
- No registration needed.
- Anonymous use may be restricted when the Horde is under high load.

### Why Register?

Registering lets you take part in the kudos system. Kudos decide your place in the queue: the more you have, the sooner your requests are processed. You can earn kudos by:

- Running a worker that processes other users' requests, and keeping it online
- Receiving monthly kudos as a thank-you for a [subscription to Haidra](https://haidra.net/donate/) (kudos still cannot be bought or sold)
- Taking part in the kudos economy on the [official Discord](https://discord.gg/3DxrhksKzn) by logging in with the bot. Use the command `/login` in any channel, click the blue button and enter your API key in the popup.

The kudos system rewards contribution. It does not gate access, and every user, anonymous ones included, can use the service's core features. The [developer's blog post on the kudos economy](https://dbzer0.com/blog/the-kudos-based-economy-for-the-koboldai-horde/) explains the reasoning.

## Contribute your GPU (Become a worker)

[Register](#getting-started-with-ai-horde) first, then run one of the workers below with your API key. The website has a [worker guide](https://aihorde.net/contribute/workers).

### Image

The officially supported image worker is [horde-worker-reGen](https://github.com/Haidra-Org/horde-worker-reGen).

### Text

- [KoboldCpp](https://github.com/LostRuins/koboldcpp) has a built-in Horde worker, including chat jobs.
- [AI-Horde-Worker](https://github.com/Haidra-Org/AI-Horde-Worker) connects an inference backend to the Horde.
- Any OpenAI-compatible backend can serve chat jobs. See [write a minimal chat worker](https://haidra-org.github.io/AI-Horde/how-to/write_a_minimal_chat_worker/) and [serve chat jobs](https://haidra-org.github.io/AI-Horde/how-to/serve_chat_jobs/).

Join the #horde channel on the [KoboldAI Discord server](https://discord.gg/XuQWadgU9k) to learn more about text workers.

## Integrate with the AI-Horde

To build a bot, app or script on the AI Horde, read the [integration readme](README_integration.md). It covers the request workflow, the endpoints for each generation type, shared keys and error handling. For Python, the [horde-sdk](https://github.com/Haidra-Org/horde-sdk) handles the request lifecycle for you. Chat clients can use the [OpenAI-compatible endpoint](#openai-compatible-chat-completions), and frontend authors can follow [add chat to a frontend](https://haidra-org.github.io/AI-Horde/how-to/add_chat_to_a_frontend/).

## Contributing to This Repository

See [CONTRIBUTING.md](CONTRIBUTING.md) for the development setup, tests, code style and documentation conventions. Changes are listed in the [changelog](CHANGELOG.md).

## Community

Questions and feedback are welcome on [Discord](https://discord.gg/3DxrhksKzn), [Mastodon](https://sigmoid.social/@stablehorde) and [Lemmy](https://lemmy.dbzer0.com/c/aihorde). Project news is posted at [aihorde.net/news](https://aihorde.net/news).

## License

The AI Horde server is free software under the [GNU Affero General Public License v3.0 or later](LICENSE). Licensing follows the [REUSE](https://reuse.software/) specification, and the license texts are in [LICENSES/](LICENSES/).
