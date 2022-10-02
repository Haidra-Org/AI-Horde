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

1. Go to this fork of the [stable diffusion webui](https://github.com/sd-webui/stable-diffusion-webui) and follow the install instructions as normal with a few differences:
1. (Optional step) Before you run webui.sh or webui.cmd. If you do not do this step, you will contribute anonymously.
   * Make a copy of `scripts/bridgeData_template.py` into `scripts/bridgeData.py`. 
   * Edit `scripts/bridgeData.py` and put details for your server such as the API key you've received, so that you can receive Kudos. 
1. Start the software with webui.(cmd|sh) as usual and add the `--bridge` argument
   * Linux: `webui.sh --bridge`
   * Windows: `webui.cmd --bridge`

# Joining the horde with multiple GPUs

To use multiple GPUs as with NVLINK workers, each has to start their own webui instance. For linux, you just need to limit the run to a specific card:

```
CUDA_VISIBLE_DEVICES=0 ./webui.sh --bridge -n "My awesome instance #1"
CUDA_VISIBLE_DEVICES=1 ./webui.sh --bridge -n "My awesome instance #2"
```
etc

## Advanced Usage: Local + Horde SD

TBD
