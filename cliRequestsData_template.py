filename = "horde_generation.png"
api_key = "0000000000"
# You can fill these in to avoid putting them as args all the time
imgen_params = {
    "n": 1,
    "width": 64*8,
    "height":64*8,
    "steps": 20,
    # denoising strength is only relevant to img2img
    "denoising_strength": 0.8,
    # You can put extra SD webui params here if you wish
}
submit_dict = {
    "prompt": "a swarm of incredibly cute stable robots, intricate, highly detailed, artstation, concept art, smooth, sharp focus, colorful scene,  in the style of don bluth, greg rutkowski, disney, and hans zatzka",
    # "prompt": "smiling wild west outlaw walking towards the camera, illustration by Riccardo Rullo",
    "nsfw": False,
    "censor_nsfw": False,
    "trusted_workers": False,
}
source_image = None
# Uncomment this line to try img2img
# Change the filename to your own image
# source_image = './db0.jpg'