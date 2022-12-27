# Stable Horde

<img style="float:right" src="{horde_img_url}/{horde_image}.jpg" width="300" /> This is a [crowdsourced distributed cluster](https://github.com/db0/AI-Horde) of [Stable Diffusion workers](https://github.com/sd-webui/nataili). If you like this service, consider [joining the horde yourself](https://github.com/db0/AI-Horde/blob/main/README_StableHorde.md)!

For more information, check [the FAQ](https://github.com/db0/AI-Horde/blob/main/FAQ.md). Also check out our sister project for text generation: [KoboldAI Horde](https://koboldai.net)

## Latest [News](/api/v2/status/news)

{news}

## Stats 

* Average Recent Performance: {avg_performance} {avg_thing_name} per second
* Total generated: {total_things} {total_things_name}
* Total requests fulfilled: {total_fulfillments}{total_fulfillments_char}
* Active [Workers](/api/v2/workers): {active_workers}
* Queue: {total_queue} requests for a total of {queued_things} {queued_things_name}

## Usage

First [Register an account](/register) which will generate for you an API key. Store that key somewhere.

   * if you do not want to register, you can use '0000000000' as api_key to connect anonymously. However anonymous accounts have the lowest priority when there's too many concurrent requests!
   * To increase your priority you will need a unique API key and then to increase your Kudos. [Read how Kudos are working](https://dbzer0.com/blog/the-kudos-based-economy-for-the-koboldai-horde/).

### GUI

* We provide [a client interface](https://dbzer0.itch.io/lucid-creations) requiring no installation and no technical expertise
* We have also a few dedicated Web UIs with even less requirements:
    * [Stable UI](https://aqualxx.github.io/stable-ui/)
    * [Diffusion UI](https://diffusionui.com/b/stable_horde)
    * [Art Bot](https://tinybots.net/artbot)

<img src="https://raw.githubusercontent.com/db0/Stable-Horde-Client/main/screenshot.png" width="500" />

### Command Line
1. Git clone [this repository](https://github.com/db0/Stable-Horde)
1. Make sure you have python3 installed
1. Open a git bash (or just bash in linux)
1. Download the cli requirements with `python -m pip install -r cli_requirements.txt --user`
1. Run `./cli_requests.py` 

You can use `./cli_requests.py -h` to see the command line arguments to use

You can make a copy of `cliRequestData_template.py` into `cliRequestData.py` and edit it, to use common variables for your generations. Command line arguments will always take precedence over `cliRequestData.py` so you can use them to tweak your generations slightly.

### Tools

* We have created some official tools with which to integrate into the Stable Horde
    * [Godot Engine plugin](https://github.com/db0/Stable-Horde-Client-Addon) to integrate Stable Horde image generation into your games.
    * [Discord Bot](https://github.com/JamDon2/ai-horde-bot) which you [can add to your own server](https://discord.com/api/oauth2/authorize?client_id=1019572037360025650&permissions=8192&scope=bot) to be able to generate via the Stable Horde for free, and allow your users to transfer kudos between them.
    * [Mastodon Bot](https://github.com/db0/mastodon-stable-horde-generate) which you can [use directly via mastodon to generate images](https://sigmoid.social/@stablehorde_generator).
* The community has made the following
    * Bots
        * [Telegram Bot](https://t.me/CraiyonArtBot)
        * [Discord Bot 1](https://harrisonvanderbyl.github.io/WriterBot/)
        * [Discord Bot 2](https://github.com/ZeldaFan0225/Stable_Horde_Discord)
    * Plugins
        * [GIMP Plugin](https://github.com/blueturtleai/gimp-stable-diffusion/tree/main/stablehorde)
        * [Krita Plugin](https://github.com/blueturtleai/krita-stable-diffusion)
        * [Unreal Engine Plugin](https://github.com/Mystfit/Unreal-StableDiffusionTools)
    * Other
        * [npm SDK 1](https://www.npmjs.com/package/@zeldafan0225/stable_horde)
        * [npm SDK 2](https://www.npmjs.com/package/stable-horde-api)

## REST API

[Full Documentation](/api)

## Services

* [Register New Account](/register)
* [Transfer Kudos](/transfer)

## Community

* Join us on [Discord](https://discord.gg/3DxrhksKzn)
* Support the development of the Stable Horde on [Patreon](https://www.patreon.com/db0) or [Github](https://github.com/db0)
* Follow us on <a rel="me" href="https://sigmoid.social/@stablehorde">Mastodon</a>

## Credits

These are the people who made this software possible.

* [Db0](https://dbzer0.com) - Development and Maintenance
* [Sygil-Dev](https://github.com/Sygil-Dev) - Worker backend code.
* [Sponsors](/sponsors) - See our complete sponsor list including our patreon supporters

And of course, everyone contributing their SD to the horde!
