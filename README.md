<!--
SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
SPDX-FileCopyrightText: 2024 Tazlin <tazlin.on.github@gmail.com>

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# What is the AI-Horde?

AI Horde is a free community service that lets anyone create AI-generated images and text. In the spirit of projects like Folding@home (sharing compute for medical research) or SETI@home (sharing compute for the search for alien signals), AI Horde lets volunteers share their computer power to help others create AI art and writing.

When you make a request - like asking for "a painting of a sunset over mountains" - the AI Horde system finds available volunteer computers that can handle your task. It's similar to how ride-sharing apps connect passengers with nearby drivers, but instead of rides, you're getting AI-generated content.

The system uses "kudos" points to keep things fair. Volunteers earn kudos when their computers help process requests, which they can use to get priority for their own requests or leave unspent to help others. Importantly, kudos can never be bought or sold - this is strictly against the Terms of Service.

What makes AI Horde special is that it's completely free and community-run, with a strong commitment to staying that way. The kudos system is specifically designed to ensure that access to these resources remains equitable. While users with more kudos get faster service, anyone can use it, even anonymously, and kudos never expire.

The AI-Horde hopes to ensure that everyone gets a chance to use these exciting AI technologies, regardless of their financial means or technical resources.

## Technical Introduction

The AI Horde is an enterprise-level ML-Ops crowdsourced distributed inference cluster for AI Models. This middleware can support both Image and Text generation. It is infinitelly scalable and supports seamless drop-in/drop-out of compute resources.
The [Public version](https://aihorde.net) allows people without a powerful GPU to use Stable Diffusion or Large Language Models like Pygmalion/Llama by relying on spare/idle resources provided by the communit and also allows non-python clients, such as games and apps, to use AI-provided generations.

The AI Horde middleware itself can also be run privately inside a closed environment within any enterprise. It can be installed within hours and can scale your ML solution within days of deployment.

For more questions, check the [FAQ](FAQ.md)


# Sponsors

[![](assets/logo_nlnet.svg)](https://nlnet.nl/project/AI-Horde/)

Based on the provided documents, here's a reworked version of the registration information:

# Getting Started with AI Horde

You can use any service powered by the AI Horde either with a registered account or anonymously. You can find a partial list of services powered by the AI-Horde on our [main website](https://aihorde.net/).


## OAuth2 Registered Account (Recommended)
> Note: The only information we store from your account is your unique ID. We do not other use your id for any purpose. See our [privacy policy](https://aihorde.net/privacy) for more details.
* Visit [AI Horde Registration](https://aihorde.net/register)
* Log in using one of the supported OAuth2 services
* Choose your username and receive your API key
* Benefits:
  * Start with higher priority in generation queues
  * Can change username or reset API key if needed
  * Maintain and track your kudos balance
  * Can recover account access through OAuth2 service
  * Minimum kudos balance of 25

## Pseudonymous Account
* Visit [AI Horde Registration](https://aihorde.net/register)
* This method is the default if you do not log in with an OAuth2 service (google, github, discord, etc)
* **Important**: If you lose your API key, the account cannot be recovered
* Cannot change username or reset API key
* Still earns and maintains kudos
* Still better priority than anonymous users

## Anonymous Usage
* Use API key '0000000000'
* Lowest priority in generation queues
* No kudos tracking
* No need to register
* Service may be restricted for anonymous users during high load


## Why Register?
The main benefit of registration is participating in the kudos system. Kudos determine your priority in the queue - the more kudos you have, the faster your requests are processed. You can earn kudos by:
* Running a worker to help process other users' requests
* Receiving kudos as a thank-you for donations (though kudos cannot be directly bought or sold)
* Being online and available as a worker

Remember: AI Horde is committed to remaining free and accessible. While the kudos system provides priority benefits, it's designed to encourage community contribution rather than commercialization. All users, even anonymous ones, can access the service's core features. Read about this on the [official developer's blog.](https://dbzer0.com/blog/the-kudos-based-economy-for-the-koboldai-horde/)


# Integration

If you want to build an integration to the AI Horde (Bot, application, scripts etc), please consult our [Integration Readme](README_integration.md)

# Community

If you have any questions or feedback, we have a vibrant community on [discord](https://discord.gg/3DxrhksKzn)

# Horde-Specific Information

Please see the individual readmes for each specific mode supported by the AI Horde.

   * [Image generation Readme](README_StableHorde.md)
   * [Text generation Readme](README_KoboldAIHorde.md)
   * [Docker Readme](README_docker.md)
