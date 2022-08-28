# KoboldAI-cluster

Turns KoboldAI into a giant crowdsourced distributed cluster

# Generating Prompts

To request the generation for a prompt, you need to send a post request like so:

```
curl -H "Content-Type: application/json" -d '{"prompt":"I entered into an argument with a clown", "params":{"max_length":16, "frmttriminc": true, "n":2}, "username":"db0", "models":["KoboldAI/fairseq-dense-13B-Nerys-v2"]}' http://dbzer0.com:5001/generate/prompt
```

The "params" dictionary is the same as the parameters you pass to the KoboldAI API in the `api/latest/generate endpoint`, the only difference is that the "prompt" is outside the "params" dictionary.

With one important difference, the "Gens per action" param `n` can be as high as you want! Each server will only handle 1 at a time, but multiple server will be able to work on your request at the same time.

Pass a username in order to track your usage. Proper authentication will come later.

Pass the desired model names in the "model" arg to allow only KAIs running one of those models to fulfil your request. If you skip the "models" arg, all KAI instances will be able to generate for you, but of course the result will vary per model.

The `max_length` and `max_content_length` params that you pass are your wish. If the KAI server checking your request has a lower limit, they will skip all generation requests with a higher wish

Once run, This request will return a UUID

```
{"id": "34a9f91a-6db5-4d4c-962f-c4d795739610"}
```

You can now request the status of this prompt using this UUID from the server

```
curl dbzer0.com:5001/generate/prompt/2a72f411-a4c3-49e1-aad4-41005e1ff769
{"finished": 1, "processing": 0, "waiting": 1, "done": false, "generations": [" as he stood before me."]}
```

Once the `finished` arg is equal to your `n`, then your request is completed.

# Joining the cluster

This repository comes with a little bridge script which you can run on your own machine (windows or linux). It will take care of communicating between KAI cluster server and your own KAI. This will allow people to use their own PCs to support the KAI cluster.

**You KoboldAI instance must be using the UNITED branch!**

* First clone this repo and then open a terminal/console and `cd` into it
* Make sure you have python3 installed
* install the requirements with pip: `python -m pip install -r requirements.txt --user`
* Edit the clientData.py file and add your own username. The `password` is not being used at the moment
* Edit the clientData.py file and add your KAI server. If it's a local instance, leave it as it is. If it's a remote instance, fill in the URL and port accordingly.
* Finally, run the script: `python bridge.py`

If all goes well, it will connect to your KAI instance and then will start polling the cluster for incoming requests.

## Other endpoints

* GET dbzer0.com:5001/usage to see how much each user has consumed this service
* GET dbzer0.com:5001/contributions to see how much each user has contributed to this service with their own resources

## Other Info

The cluster does not save any prompts nor generations locally. It's all stored in memory.
(Not implemented yet) Furthermore, requested prompts and their generations are wiped after 10 minutes of inactivity

The bridge also does not save any prompts, but of course this is not under my control as the bridges run locally on the various cluster nodes. As such, you should not prompt with anything you do not want others to see!

This system stores how many tokens you requested to generate, and how many your own servers have generated for others. This is not used yet, but eventually this will be how we balance resources among the users.