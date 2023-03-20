# AI Horde

<img style="float:right" src="{horde_img_url}/{horde_image}.jpg" width="300" /> This is a [crowdsourced distributed cluster](https://github.com/db0/AI-Horde) of [Image generation workers](https://github.com/db0/AI-Horde-Worker) and [text generation workers](https://github.com/KoboldAI/KoboldAI-Client). If you like this service, consider [joining the horde yourself](https://github.com/db0/AI-Horde/blob/main/README_StableHorde.md#joining-the-horde)!

For more information, check [the FAQ](https://github.com/db0/AI-Horde/blob/main/FAQ.md). Finally you can also follow the [main developer's blog](https://dbzer0.com)

## Latest [News](/api/v2/status/news)

{news}

## Stats 

### Image Generation
* Average Recent Performance: {avg_performance} {avg_thing_name} per second. 
* Total generated: {total_image_things} {total_total_image_things_name}. 
* Total image requests fulfilled: {total_image_fulfillments}{total_image_fulfillments_char}.
* Active [Dreamers](/api/v2/workers?type=image)/Threads: {image_workers}/{image_worker_threads}
* Queue: {total_image_queue} requests for a total of {queued_image_things} {queued_image_things_name}. 
### Text Generation
* Average Recent Performance: {avg_text_performance} {avg_text_thing_name} per second. 
* Total generated: {total_text_things} {total_text_things_name}. 
* Total texts requests fulfilled: {total_text_fulfillments}{total_text_fulfillments_char}.
* Active [Scribes](/api/v2/workers?type=text)/Threads: {text_workers}/{text_worker_threads}
* Queue: {total_text_queue} requests for a total of {queued_text_things} {queued_text_things_name}. 
### Image Alchemy
* Total processed: {total_forms}{total_forms_char}.
* Active [Alchemists](/api/v2/workers?type=interrogation)/Threads: {interrogation_workers}/{interrogation_worker_threads}
* Queue: {total_forms_queue} alchemy forms.

## Usage

First [Register an account](/register) which will generate for you an API key. Store that key somewhere.

   * if you do not want to register, you can use '0000000000' as api_key to connect anonymously. However anonymous accounts have the lowest priority when there's too many concurrent requests!
   * To increase your priority you will need a unique API key and then to increase your Kudos. [Read how Kudos are working](https://dbzer0.com/blog/the-kudos-based-economy-for-the-koboldai-horde/).

### GUI

* We provide [a client interface](https://dbzer0.itch.io/lucid-creations) requiring no installation and no technical expertise
* We have also a few dedicated Web UIs with even less requirements:
    * [Stable UI](https://aqualxx.github.io/stable-ui/)
    * [Art Bot](https://tinybots.net/artbot)
    * [Diffusion UI](https://diffusionui.com/b/stable_horde)
    * [AAAI UI](https://artificial-art.eu/)
* There are also mobile apps:
    * [Stable Horde Flutter](https://ppiqr.app.link/download) (iOS + Android app)

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
    * [Discord Bot](https://github.com/ZeldaFan0225/Stable_Horde_Discord) which you [can add to your own server](https://discord.com/api/oauth2/authorize?client_id=1019572037360025650&permissions=8192&scope=bot) to be able to generate via the Stable Horde for free, and allow your users to transfer kudos between them.
    * [Mastodon Bots](https://github.com/db0/mastodon-stable-horde-generate) which you can use directly via Activity Pub to generate images.
        * <a rel="me" href="https://sigmoid.social/@stablehorde_generator">Sigmoid.social</a>
        * <a rel="me" href="https://hachyderm.io/@haichy">Hachyderm.io</a>
    * [Reddit Bot](https://github.com/db0/reddit-stable-horde-generate) which you can use [directly via reddit](https://www.reddit.com/user/StableHorde/comments/znhtaw/faq/) to generate images.

* The community has made the following
    * Bots
        * [CraiyonArt Bot](https://t.me/CraiyonArtBot) (Telegram)
        * [WriterBot](https://harrisonvanderbyl.github.io/WriterBot/) (Discord)
        * [Turing Bot](https://github.com/MrlolDev/turing-bot) (Discord)
        * [AI Horde Bot](https://github.com/JamDon2/ai-horde-bot) (Discord, Obsolete)
    * Plugins
        * [GIMP Plugin](https://github.com/blueturtleai/gimp-stable-diffusion/tree/main/stablehorde)
        * [Krita Plugin](https://github.com/blueturtleai/krita-stable-diffusion)
        * [Unreal Engine Plugin](https://github.com/Mystfit/Unreal-StableDiffusionTools)
        * [Automatic 1111 Web UI](https://github.com/natanjunges/stable-diffusion-webui-stable-horde)
        * [Blender Plugin](https://github.com/benrugg/AI-Render)
        * [Chrome Accessibility Plugin](https://chrome.google.com/webstore/detail/genalt-generated-alt-text/ekbmkapnmnhhgfmjdnchgmcfggibebnn)
    * Other
        * [npm SDK](https://www.npmjs.com/package/@zeldafan0225/ai_horde)

## REST API

[Full Documentation](/api)

If you are developing a paid or ad-based integration with the Stable Horde, we request that you use part of your profits to support the horde. If your app is solely reliant on the volunteer resources of the horde, we expect at least 50% of those should go to supporting the horde itself, preferrably by onboarding your own workers. If the horde is merely an option among many, we suggest you assign some workers to the horde depending on how much it's being utilized by your client base.

## Services

* [Register New Account](/register)
* [Transfer Kudos](/transfer)

## Community

* Join us on [Discord](https://discord.gg/3DxrhksKzn)
* Support the development of the Stable Horde on [Patreon](https://www.patreon.com/db0) or [Github](https://github.com/db0)
* Follow us on <a rel="me" href="https://sigmoid.social/@stablehorde">Mastodon</a>
* Subscribe to the [Division by Zer0 blog](https://dbzer0.com/)

## Credits

These are the people who made this software possible.

* [Db0](https://dbzer0.com) - Development and Maintenance
* [Sponsors](/sponsors) - See our complete sponsor list including our patreon supporters

And of course, everyone contributing their SD to the horde!
