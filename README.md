<!--
SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
SPDX-FileCopyrightText: 2024 Tazlin <tazlin.on.github@gmail.com>

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# AI-Horde: Community-Powered AI Generation

The [AI Horde](https://github.com/Haidra-Org/haidra-assets/blob/main/docs/definitions.md#ai-horde) is a free community service that lets anyone create [AI-generated images](https://github.com/Haidra-Org/haidra-assets/blob/main/docs/definitions.md#image-generation) and [text](https://github.com/Haidra-Org/haidra-assets/blob/main/docs/definitions.md#text2text). In the spirit of projects like Folding@home (sharing compute for medical research) or SETI@home (sharing compute for the search for alien signals), AI Horde lets volunteers share their computer power through [workers](https://github.com/Haidra-Org/haidra-assets/blob/main/docs/definitions.md#worker) to help others create AI art and writing.

When you make a [request](https://github.com/Haidra-Org/haidra-assets/blob/main/docs/definitions.md#request) - like asking for "a painting of a sunset over mountains" - the AI Horde system finds available volunteer computers that can handle your [job](https://github.com/Haidra-Org/haidra-assets/blob/main/docs/definitions.md#job). It's similar to how ride-sharing apps connect passengers with nearby drivers, but instead of rides, you're getting AI-generated content.

The system uses "[kudos](https://github.com/Haidra-Org/haidra-assets/blob/main/docs/kudos.md)" points to keep things fair. [Workers](https://github.com/Haidra-Org/haidra-assets/blob/main/docs/definitions.md#worker) earn kudos when their computers help process requests, which they can use to get priority for their own requests or leave unspent to help others. Importantly, kudos can never be bought or sold - this is strictly against the [Terms of Service](https://github.com/Haidra-Org/haidra-assets/blob/main/docs/definitions.md#terms-of-service). If you would like to learn more about kudos, see [our detailed explanation](https://github.com/Haidra-Org/haidra-assets/blob/main/docs/kudos.md).

What makes AI Horde special is that it's completely free and community-run, with a strong commitment to staying that way. The kudos system is specifically designed to ensure that access to these resources remains equitable. While [users](https://github.com/Haidra-Org/haidra-assets/blob/main/docs/definitions.md#user) with more kudos get faster service, anyone can use it for free, even [anonymously](https://github.com/Haidra-Org/haidra-assets/blob/main/docs/definitions.md#anonymous), and kudos never expire.

The AI-Horde hopes to ensure that everyone gets a chance to use these exciting AI technologies, regardless of their financial means or technical resources. You can read more about why we do this in the [motivations document](https://github.com/Haidra-Org/haidra-assets/blob/main/docs/why.md).

## Sponsors

[![NLnet logo](assets/logo_nlnet.svg)](https://nlnet.nl/project/AI-Horde/)

## Table of Contents

- [AI-Horde: Community-Powered AI Generation](#ai-horde-community-powered-ai-generation)
      - [Table of Contents](#table-of-contents)
- [Sponsors](#sponsors)
- [Technical Introduction](#technical-introduction)
  - [Key Features](#key-features)
  - [Public Version](#public-version)
  - [Private Deployment](#private-deployment)
  - [Technologies Used](#technologies-used)
  - [How It Works](#how-it-works)
- [Getting Started with AI Horde](#getting-started-with-ai-horde)
  - [OAuth2 Registered Account (Recommended)](#oauth2-registered-account-recommended)
  - [Pseudonymous Account](#pseudonymous-account)
  - [Anonymous Usage](#anonymous-usage)
  - [Why Register?](#why-register)
- [Contribute your GPU (Become a worker)](#contribute-your-gpu-become-a-worker)
  - [Image](#image)
  - [Text](#text)
- [Integrate with the AI-Horde](#integrate-with-the-ai-horde)
- [Community](#community)

## Technical Introduction

The AI Horde is an enterprise-level ML-Ops crowd-sourced distributed inference cluster for AI Models. This middleware supports both image and text generation, making it highly versatile. It is designed to be highly scalable, allowing for seamless drop-in/drop-out of compute resources.

### Key Features

- **Scalability**: The system can scale effortlessly to accommodate varying workloads, ensuring efficient use of available resources.
- **Flexibility**: Supports both image and text generation, catering to a wide range of AI applications.
- **Community-Powered**: Relies on spare/idle resources provided by the community, making advanced AI accessible to everyone.

### Public Version

The [public version](https://aihorde.net) allows individuals without powerful GPUs to use advanced AI models like Stable Diffusion or Large Language Models (LLMs) such as Pygmalion/Llama. This is achieved by leveraging the spare computing power provided by the community, running software we call '[workers](https://github.com/Haidra-Org/haidra-assets/blob/main/docs/definitions.md#worker)'. Additionally, it supports clients, such as third party websites, games and apps, to utilize AI-generated content through a [REST API](https://aihorde.net/api/). You can see the public instance's performance and application statistics on [our grafana instance](https://grafana.aihorde.net/d/jSb16YLVk/performance?orgId=1).

### Private Deployment

The AI Horde middleware can also be deployed privately within any enterprise environment. It can be installed within hours and can scale your ML solution within days of deployment, providing a robust and flexible solution for enterprise-level AI needs.

### Technologies Used

- **Python**: 3.9 or later
- **PostgreSQL**
- **Redis**: For caching
- **Docker**- support for full containerization

### How It Works

For a high-level overview of how the AI Horde operates, including diagrams of the request/job lifecycle, see the [request/job lifecycle explanation](https://github.com/Haidra-Org/haidra-assets/blob/main/docs/workers.md).

There are also individual readmes for each specific mode supported by the AI Horde.

- [Image generation Readme](README_StableHorde.md)
- [Text generation Readme](README_KoboldAIHorde.md)
- [Docker Readme](README_docker.md)

For other questions, check the [FAQ](FAQ.md).

# Getting Started with AI Horde

You can use any service powered by the AI Horde either with a registered account or anonymously. You can find a partial list of services powered by the AI-Horde on our [main website](https://aihorde.net/).

## OAuth2 Registered Account (Recommended)

> Note: The only information we store from your account is your unique ID. We do not use your id for any other purpose. See our [privacy policy](https://aihorde.net/privacy) for more details.

- Visit [AI Horde Registration](https://aihorde.net/register)
- Log in using one of the supported OAuth2 services
- Choose your username and receive your API key
- Benefits:
  - Start with higher priority in generation queues
  - Can change username or reset API key if needed
  - Maintain and track your kudos balance
  - Can recover account access through OAuth2 service
  - Minimum kudos balance of 25

## Pseudonymous Account

- Visit [AI Horde Registration](https://aihorde.net/register)
- This method is the default if you do not log in with an OAuth2 service (google, github, discord, etc)
- **Important**: If you lose your API key, the account cannot be recovered
- Cannot change username or reset API key
- Still earns and maintains kudos
- Still better priority than anonymous users

## Anonymous Usage

- Use API key '0000000000'
- Lowest priority in generation queues
- No kudos tracking
- No need to register
- Service may be restricted for anonymous users during high load

## Why Register?

The main benefit of registration is participating in the kudos system. Kudos determine your priority in the queue - the more kudos you have, the faster your requests are processed. You can earn kudos by:

- Running a worker to help process other users' requests
- Receiving kudos as a thank-you for donations (though kudos cannot be directly bought or sold)
- Being online and available as a worker
- Participating in the kudos economy on the [official discord](https://discord.gg/3DxrhksKzn) by logging in with the bot. (Use the command `/login` in any channel, click the blue button and enter your API key at the popup).

Remember: AI Horde is committed to remaining free and accessible. While the kudos system provides priority benefits, it's designed to encourage community contribution rather than commercialization. All users, even anonymous ones, can access the service's core features. Read about this on the [official developer's blog.](https://dbzer0.com/blog/the-kudos-based-economy-for-the-koboldai-horde/)

## Contribute your GPU (Become a worker)

Be sure to [register](#getting-started-with-ai-horde) first.

### Image

The officially supported way to add your GPU for image generation is by running the worker software [horde-worker-reGen](https://github.com/Haidra-Org/horde-worker-reGen).

### Text

There are multiple ways to contribute your GPU for text generation:

- [AI-Horde-Worker](https://github.com/Haidra-Org/AI-Horde-Worker) can connect to inference backends and 'bridge' you to the AI-Horde
- [KoboldAI](https://github.com/henk717/KoboldAI)
- [KoboldCPP](https://github.com/lostruins/koboldcpp)
- [Aphrodite Engine](https://github.com/PygmalionAI/aphrodite-engine)
- ... and many others. Join the discussion in the #horde channel of the [KoboldAI Discord Server](https://discord.gg/XuQWadgU9k) to learn more.

## Integrate with the AI-Horde

If you want to build an integration to the AI Horde (Bot, application, scripts etc), please consult our [Integration Readme](README_integration.md)

## Community

If you have any questions or feedback, we have a vibrant community on [discord](https://discord.gg/3DxrhksKzn)
