import requests, json, os, time, argparse, base64
from logger import logger, set_logger_verbosity, quiesce_logger, test_logger
from PIL import Image, ImageFont, ImageDraw, ImageFilter, ImageOps

#curl -H "Content-Type: application/json" -d '{"prompt":"a horde of cute stable robots in a sprawling server room repairing a massive mainframe, intricate, highly detailed, artstation, concept art, smooth, sharp focus,
#colorful scene,  in the style of don bluth, greg rutkowski, disney, and hans zatzka", "params":{"n":1}, "api_key":"3P1JO2XY8Lt-Xn2dEZFtRg"}' https://stablehorde.net/api/v1/generate/sync

imgen_params = {
    "n":1,
    "height": 64*8,
    "width": 64*8,
}

submit_dict = {
    "prompt":"a horde of cute stable robots in a sprawling server room repairing a massive mainframe, intricate, highly detailed, artstation, concept art, smooth, sharp focus, colorful scene,  in the style of don bluth, greg rutkowski, disney, and hans zatzka",
    "api_key":"0000000000",
    "params": imgen_params,
}

@logger.catch
def test():
    submit_req = requests.post('https://stablehorde.net/api/v1/generate/sync', json = submit_dict)
    if submit_req.ok:
        results = submit_req.json()
        b64img = results[0]["img"]
        base64_bytes = b64img.encode('utf-8')
        img_bytes = base64.b64decode(base64_bytes)
        img = Image.frombytes('RGB', (imgen_params["width"],imgen_params["height"]), img_bytes, "raw")
        img.save("test.png")
    else:
        print(submit_req.text)

test()