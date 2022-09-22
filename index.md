# Stable Horde

<img style="float:right" src="https://raw.githubusercontent.com/db0/Stable-Horde/main/img/{stable_image}.jpg" width="300" /> This is a [crowdsourced distributed cluster](https://github.com/db0/Stable-Horde) of [Stable Diffusion workers](https://github.com/db0/stable-diffusion-webui). If you like this service, consider joining the horde yourself!

Also check out our sister project: [KoboldAI Horde](https://koboldai.net)

## Stats 

* Average Recent Performance: {avg_performance} Megapixelsteps per second
* Total GPS generated: {total_pixels} Gigapixelsteps
* Total requests fulfilled: {total_fulfillments}
* Active [Servers](/api/v1/servers): {active_servers}
* Current Request Queue: {total_queue}

## Usage

First [Register an account](/register) which will generate for you an API key. Store that key somewhere.

   * if you do not want to register, you can use '0000000000' as api_key to connect anonymously. However anonymous accounts have the lowest priority when there's too many concurrent requests!
   * To increase your priority you will need a unique API key and then to increase your Kudos. [Read how Kudos are working](https://dbzer0.com/blog/the-kudos-based-economy-for-the-koboldai-horde/).

### GUI

We provide [a client interface](https://dbzer0.itch.io/stable-horde-client) requiring no installation and no technical expertise

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

We provide a [Godot Engine plugin](https://github.com/db0/Stable-Horde-Client-Addon) to integrate Stable Horde image generation into your games.

## Services

* [Register New Account](/register)
* [Transfer Kudos](/transfer)

## Community

Join us on [Discord](https://discord.gg/3DxrhksKzn)

## Credits

These are the people who made this sotware possible.

* [Db0](https://dbzer0.com) - Development and Maintenance

And of course, everyone contributing their SD to the horde!
