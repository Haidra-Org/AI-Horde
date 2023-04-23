filename = "horde_alchemy" # The file will be appended with the alchemy type and then the extension
api_key = "0000000000"
source_image = './db0.jpg'
# Uncomment any of the below you want to use
submit_dict = {
    "trusted_workers": False,
    "slow_workers": True,
    "forms": [
        {"name": "caption"},
        # {"name": "interrogation"},
        # {"name": "nsfw"},
        # {"name": "GFPGAN"},
        # {"name": "RealESRGAN_x4plus"},
        # {"name": "CodeFormers"},
        # {"name": "strip_background"},
        # {"name": "RealESRGAN_x2plus"},
        # {"name": "NMKD_Siax"},
        # {"name": "4x_AnimeSharp"},
    ]
}
