<!--
SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# AI Horde

The AI Horde is an enterprise-level ML-Ops crowdsourced distributed inference cluster for AI Models. This middleware can support both Image and Text generation. It is infinitelly scalable and supports seamless drop-in/drop-out of compute resources.
The [Public version](https://aihorde.net) allows people without a powerful GPU to use Stable Diffusion or Large Language Models like Pygmalion/Llama by relying on spare/idle resources provided by the communit and also allows non-python clients, such as games and apps, to use AI-provided generations.

The AI Horde middleware itself can also be run privately inside a closed environment within any enterprise. It can be installed within hours and can scale your ML solution within days of deployment.

For more questions, check the [FAQ](FAQ.md)

# Sponsors

[![](assets/logo_nlnet.svg)](https://nlnet.nl/project/AI-Horde/)

# Registering

To use the horde you need to have a registered account, or use anonymous mode.

To register an account, go to the AI Horde website:
   * [AI Horde Registration](https://aihorde.net/register)

and login with one of the available services. Once you do you'll see a form where you can put a username. Add one in and it will automatically store a user object for you and provide an API key to identify you.

Store this API key and use it for your client or bridge.

By logging in first, you can change your username and API key at any time.
We don't store any identifiable information other than the ID string sent by the oauth for your user. We only use this for user uniqueness, and no other purpose.

If you want, you can also create a pseudonymous account, without logging in with oauth. However *we will not maintain such accounts*. If you lose access to it, you'll have to make a new one. If someone copies your API Key, they can impersonate you. You cannot change the username or API key anymore etc. If you don't want these risks, login to one of the available services instead.

If you do not want to login even with a pseudonymous account, you can use this service anonymously by using '0000000000' as your API key. However your usage and contributions will be not be tracked. Be aware that if this service gets too overloaded, anonymous mode might be turned off!

The point of registering is to track your usage and your contributions. The more you contribute to the Horde, the more priority you have. [Read about this here](https://dbzer0.com/blog/the-kudos-based-economy-for-the-koboldai-horde/)

## Integration

If you want to build an integration to the AI Horde (Bot, application, scripts etc), please consult our [Integration Readme](README_integration.md)

# Community

If you have any questions or feedback, we have a vibrant community on [discord](https://discord.gg/3DxrhksKzn)

# Horde-Specific Information


Please see the individual readmes for each specific mode supported by the AI Horde.

   * [Image generation Readme](README_StableHorde.md)
   * [Text generation Readme](README_KoboldAIHorde.md)
   * [Docker Readme](README_docker.md)
