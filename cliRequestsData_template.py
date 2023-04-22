filename = "horde_generation.png"
api_key = "0000000000"
source_image = None
# You can fill these in to avoid putting them as args all the time
imgen_params = {
    "n": 1,
    "width": 64*8,
    "height":64*8,
    "steps": 30,
    "denoising_strength": 0.6,
    "sampler_name": "k_euler",
    "cfg_scale": 7.5,
    "karras": True,
    "tiling": False,
    "hires_fix": False,
    # Uncomment the below line to pass a specific seed
    # "seed": "the little seed that could",
    # Uncomment the below line to pass post-processors. Only pass the PPs you need
    # "post_processing": ["GFPGAN", "RealESRGAN_x4plus", "RealESRGAN_x4plus_anime_6B", "CodeFormers"],
    # Uncomment the below line to pass a ControlNet. Make sure you write a valid Control Type
    # "control_type": "canny",
}
submit_dict = {
    # "prompt": "a swarm of incredibly cute stable robots, intricate, highly detailed, artstation, concept art, smooth, sharp focus, colorful scene,  in the style of don bluth, greg rutkowski, disney, and hans zatzka",
    "prompt": "grinning wild west outlaw walking towards the camera, gunbelt, detailed face, explosion in background",
    "nsfw": False,
    "censor_nsfw": False,
    "trusted_workers": False,
    "slow_workers": True,
    # Put the models you allow to fulfil your request in reverse order of priority. The last model in this list is the most likely to be chosen
    "models": [
        "stable_diffusion",
        # "waifu_diffusion",
        # "Yiffy",
        # "trinart",
    ]
}
# Uncomment this line to try img2img
# Change the filename to your own image
# source_image = './db0.jpg'
# Uncomment these three lines to try inpainting. If the source_mask is not provided, the image sent has to have areas already erased-to-alpha
# source_image = './inpaint_original.png'
# source_processing = 'inpainting'
# source_mask = './inpaint_mask.png'
