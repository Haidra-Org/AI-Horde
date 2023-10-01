# AI Horde LLM Generation

Information specific to the Text generation

# Generating Prompts

## GUI

The easiest way to start using the [KoboldAI Lite](https://lite.koboldai.net) requiring no installation and no technical expertise

## Command Line

I have provided [a small python script](https://github.com/Haidra-Org/AI-Horde-CLI) with which you can use to call the horde. Please check the [README](https://github.com/Haidra-Org/AI-Horde-CLI/blob/main/README.md) for usage instructions

## Using KoboldAI client

KoboldAI Client supports using the AI Horde directly from its interface. To use it go to the AI menu on the top left, then select Online Services > KoboldAI Horde. In the next window that opens, you have to fill in the url of the Horde (You can use `https://aihorde.net`) and then type a username. If the models window hasn't appeared yet, click away from the textbox and it should. You can select one of more models that you want to use for your generations, or All, if you don't care. Then finally click `Load`.

![](gui_select.png)

You can also start KAI directly in horde mode by using the command line in the `play.(sh|bat)` file. Pass the arguments to start a KAI instance in cluster mode like so (Change "0000000000" to your own API KEY, if you have one.)

LINUX

```bash
APIKEY=0000000000
./play.sh --path https://aihorde.net --model CLUSTER --apikey ${APIKEY}
```

WINDOWS


```bash
play.bat --path https://aihorde.net --model CLUSTER --apikey 0000000000
```

This will use any available model on the cluster. If you want to use only specific models, pass the wanted modules via one or more `req_model` args. Example `--req_model "KoboldAI/fairseq-dense-13B-Nerys-v2" --req_model "KoboldAI/fairseq-dense-2.7B-Nerys"`

Once the KAI starts in cluster mode, any request will be sent to the AI Horde

# Joining the AI Horde

Anyone can convert their own PC into a worker which generates or interrogates images for other people in the horde and gains kudos for doing so. To do so, they need to run a software we call the AI Horde Worker, which bridges your Stable Diffusion inference to the AI Horde via REST API.

We have prepared a [very simple installation procedure for running the bridge on each OS](https://github.com/Haidra-Org/AI-Horde-Worker#readme).

## KoboldAI Client UI2

**You KoboldAI client must be using the UNITED branch!**

The United branch of KoboldAI now supports becoming a worker for the AI Horde directly from its interface. To use it you need to switch to UI2, load a model, and then in your settings sidebar, fill out your worker name and API key.

# Other Info

The cluster does not save any prompts nor generations locally. It's all stored in memory. Furthermore, requested prompts and their generations are wiped after 20 minutes of inactivity

The bridge also does not save any prompts, but of course this is not under my control as the bridges run locally on the various cluster nodes. As such, you should not prompt with anything you do not want others to see!

This system stores how many tokens you requested to generate, and how many your own servers have generated for others. This is not used yet, but eventually this will be how we balance resources among the users.

# Advanced Usage: Local + Horde KAI

If you want to both play with KAI AND share resources with the community, you can achieve this by running two instances of KAI side by side. One normal one, and one in cluster mode. That way when you're using the clustered KAI, you will ensure there's always at least one instance to serve you (your own), while also taking advantage of any other instances that are onboarded at the time.

1. start KAI as you would normally do with `play.(sh|bat)` and load the model you want.
2. open a terminal window at your KAI installation, and now run `play.(sh|bat)` using a different port in CLUSTER mode. This will start open another KAI window, while leaving your model-loaded KAI intact. Make sure the `req_model` you pass, includes the model loaded in your own KAI instance.

```bash
play.bat --port 5002 --path https://aihorde.net --model CLUSTER --apikey 0000000000 --req_model "KoboldAI/fairseq-dense-13B-Nerys-v2" --req_model "KoboldAI/fairseq-dense-2.7B-Nerys"
```

Now use the CLUSTER KAI to play. You will see the requests being fulfilled by the model-loaded KAI after you press the button.
