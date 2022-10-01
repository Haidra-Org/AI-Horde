# Stable Horde information

Information specific to the Stable Diffusion image generation

# Generating Prompts

## GUI

We provide [a client interface](https://dbzer0.itch.io/stable-horde-client) requiring no installation and no technical expertise

<img src="https://raw.githubusercontent.com/db0/Stable-Horde-Client/main/screenshot.png" width="500" />


## Command Line

I have provided a small python script with which you can use to call the horde.

1. Git clone [this repository](https://github.com/db0/Stable-Horde)
1. Make sure you have python3 installed
1. Open a git bash (or just bash in linux)
1. Download the cli requirements with `python -m pip install -r cli_requirements.txt --user`
1. Run `./cli_requests.py` 

You can use `./cli_requests.py -h` to see the command line arguments to use

You can make a copy of `cliRequestData_template.py` into `cliRequestData.py` and edit it, to use common variables for your generations. Command line arguments will always take precedence over `cliRequestData.py` so you can use them to tweak your generations slightly.

## REST API

[Full Documentation](https://stablehorde.net/api/v1)

![](api_screenshot.png)

You can also use the REST API directly. Be aware that this will return a base64 encoded image, so it will flood your output. This is not recommended unless you know what you're doing!

```
curl -H "Content-Type: application/json" -H "apikey: 0000000000" -d '{"prompt":"A horde of stable robots", "params":{"n":1, "width": 256, "height": 256}}' https://stablehorde.net/api/v2/generate/sync
```

The "params" dictionary is the same as use by the Stable API Webui. Documentation will be forthcoming.

Pass an API Keyin order to track your usage.

## Specifying servers

You can optionally specify only specific servers to generate for you. Grab one or more server IDs from `/servers` and then send it with your payload as a list in the "servers" arg. Your generation will only be fulfiled by servers with the specified IDs


# Joining the horde

1. Go to this fork of the [stable diffusion webui](https://github.com/db0/stable-diffusion-webui) and follow the install instructions as normal but do not start it. If you already the upstream version of this repo, you can simply change your origin to my fork and pull again.
1. (Optional) Make a copy of `scripts/bridgeData_template.py` into `scripts/bridgeData_template.py`. If you do not do this step, you will contribute anonymously.
1. (Optional) Edit `scripts/bridgeData_template.py` and put details for your server such as the API key you've received, so that you can receive Kudos. If you do not do this step, you will contribute anonymously.
1. Start the software with webui.(cmd|sh) as usual.

My fork has been modified to start in bridge mode, but you can edit `relauncher.py` to make it start as a normal webui as well.


## Advanced Usage: Local + Horde SD

TBD