# Stable Horde

<img style="float:right" src="https://github.com/db0/Stable-Horde/blob/master/img/{kobold_image}.png?raw=true" width="300" /> This is a crowdsourced distributed cluster of [Stable Diffusion generators](https://github.com/db0/stable-diffusion-webui). If you like this service, consider joining the horde yourself!

Also check our sister project: [KoboldAI Horde](https://koboldai.net)

## Stats 

* Average Recent Performance: {avg_performance} Kilopixels per second
* Total pixels generated: {total_pixels} Megapixels
* Total requests fulfilled: {total_fulfillments}
* Active [Servers](/api/v1/servers): {active_servers}
* Current Request Queue: {total_queue}

## Usage

First [Register an account](/register) which will generate for you an API key. Store that key somewhere.

   * if you do not want to register, you can use '0000000000' as api_key to connect anonymously. However anonymous accounts have the lowest priority when there's too many concurrent requests!
   * To increase your priority you will need a unique API key and then to increase your Kudos. [Read how Kudos are working](https://dbzer0.com/blog/the-kudos-based-economy-for-the-koboldai-horde/).

### Command Line
1. Git clone [this repository](https://github.com/db0/Stable-Horde)
1. Make sure you have python3 installed
1. Open a git bash (or just bash in linux)
1. Download the cli requirements with `python -m pip install -r cli_requirements.txt --user`
1. Run `./cli_requests.py` 

You can use `./cli_requests.py -h` to see the command line arguments to use

You can make a copy of `cliRequestData_template.py` into `cliRequestData.py` and edit it, to use common variables for your generations. Command line arguments will always take precedence over `cliRequestData.py` so you can use them to tweak your generations slightly.

### GUI

Coming Soon

## Services

* [Register New Account](/register)
* [Transfer Kudos](/transfer)

## Community

TBD 

## Credits

These are the people who made this sotware possible.

* [Db0](https://dbzer0.com) - Development and Maintenance

And of course, everyone contributing their SD to the horde!
