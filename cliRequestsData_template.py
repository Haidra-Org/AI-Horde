filename = "horde_generation.png"
api_key = "0000000000"
source_img = None
# You can fill these in to avoid putting them as args all the time
imgen_params = {
    "n": 1,
    "width": 64*8,
    "height":64*8,
    "steps": 50,
    "denoising_strength": 0.6,
    "sampler_name": "k_lms",
    # Uncomment the below line to pass a specific seed
    # "seed": "the little seed that could",
    # You can put extra SD webui params here if you wish
}
submit_dict = {
    # "prompt": "a swarm of incredibly cute stable robots, intricate, highly detailed, artstation, concept art, smooth, sharp focus, colorful scene,  in the style of don bluth, greg rutkowski, disney, and hans zatzka",
    "prompt": "grinning wild west outlaw walking towards the camera, gunbelt, detailed face, explosion in background",
    "nsfw": False,
    "censor_nsfw": True,
    "trusted_workers": False,
    # Put the models you allow to fulfil your request in reverse order of priority. The last model in this list is the most likely to be chosen
    "models": [
        # "waifu_diffusion",
        "stable_diffusion"
    ]
}
# Uncomment this line to try img2img
# Change the filename to your own image
# source_image = './db0.jpg'