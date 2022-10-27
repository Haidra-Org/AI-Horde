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

You can make a copy of `cliRequestsData_template.py` into `cliRequestData.py` and edit it, to use common variables for your generations. Command line arguments will always take precedence over `cliRequestData.py` so you can use them to tweak your generations slightly.

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

Anyone can convert their own GPU into a worker which generates images for other people in the horde and gains kudos for doing so. To do so, they need to run a software we call the Horde Bridge, which bridges your Stable Diffusion installation to the Horde via REST API.

We have prepared a very simple installation procedure for running the bridge on each OS.

If this is the first time you run the bridge, afterwards continue to the "Initial Setup" section below

## Windows Steps
1. Go to [this repository](https://github.com/sd-webui/nataili) and download [the zipped version](https://github.com/sd-webui/nataili/archive/refs/heads/main.zip)
1. Extract it to any folder of your choice
1. Run `horde-bridge.cmd` by double-clicking on it from your explorer of from a `cmd` terminal.

## Linux Steps
Open a bash terminal and run these commands (just copy-paste them all together)

```bash
git clone https://github.com/sd-webui/nataili.git
cd nataili
horde-bridge.sh
```

## Initial Setup

The very first time you run the bridge, after it downloads all the python dependencies, it will take you through a small interactive setup. Simply follow the instructions as you see on the terminal and type your answer to the prompts.

In order for your worker to work, it needs to download a stable diffusion model. To do that, you will need to register a free account at https://huggingface.co. You will need to put your username and password for it when prompted. You will also need to accept the license of the model you're about to download, so after logging in to huggingface, visit https://huggingface.co/runwayml/stable-diffusion-v1-5 and accept the license presented within.

Once your models are downloaded, it will ask you to setup your bridgeData.py. Allow it to do so and it will exit. At that point, open `bridgeData.py` with a text editor such as notepad or nano, and simply fill in at least:
   * Your worker name
   * Your stable horde API key

Read the comments in this file and fill in the other fields as wanted. 

The simply rerun the `horde-bridge` script and the worker will start accepting job commissions and sending them to the horde

## Updates

The stable horde workers are under constant improvement. In case there is more recent code to use follow these steps to update

### git

Use this approach if you cloned the original repository using `git clone`

1. Open a `powershell`, `cmd` or `bash` terminal depending on your OS
1. navigate to the folder you have the nataili repository installed
1. run `git pull`

Afterwards run the `horde-bridge` script for your OS as usual.

### zip

Use this approach if you downloaded the git repository as a zip file and extracted it somewhere.

1. Download the repository from github as a zip file
1. Extract its contents into the same the folder you have the nataili repository installed, overwriting any existing files

Afterwards run the `horde-bridge` script for your OS as usual.

## Stopping

To stop the bridge, in the terminal in which it's running, simply press `Ctrl+C` together

## (Re)Starting

In case the bridge is stopped, you can start it by simply running the `horde-bridge` script for your OS

## Joining the horde with multiple GPUs

To use multiple GPUs as with NVLINK workers, each has to start their own webui instance. For linux, you just need to limit the run to a specific card:

```
CUDA_VISIBLE_DEVICES=0 ./horde-bridge.sh -n "My awesome instance #1"
CUDA_VISIBLE_DEVICES=1 ./horde-bridge.sh -n "My awesome instance #2"
```
etc
