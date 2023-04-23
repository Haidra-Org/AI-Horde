api_key = "0000000000"
# You can fill these in to avoid putting them as args all the time
txtgen_params = {
    # "n": 1,
    "max_context_length": 1024,
    "max_length": 80,
}

submit_dict = {
    "prompt": "a horde of cute kobolds furiously typing on typewriters",
    "trusted_workers": False,
    "slow_workers": True,
    # Put the models you allow to fulfil your request in reverse order of priority. The last model in this list is the most likely to be chosen
    "models": [
        "PygmalionAI/pygmalion-6b",
    ],
    "workers": [
    ]
    
}
