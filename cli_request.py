import requests, json, os, time, argparse, base64
from logger import logger, set_logger_verbosity, quiesce_logger, test_logger
from PIL import Image, ImageFont, ImageDraw, ImageFilter, ImageOps

arg_parser = argparse.ArgumentParser()
arg_parser.add_argument('-n', '--amount', action="store", required=False, type=int, help="The amount of images to generate with this prompt")
arg_parser.add_argument('-p','--prompt', action="store", required=False, type=str, help="The prompt with which to generate images")
arg_parser.add_argument('-w', '--width', action="store", required=False, type=int, help="The width of the image to generate. Has to be a multiple of 64")
arg_parser.add_argument('-l', '--height', action="store", required=False, type=int, help="The length of the image to generate. Has to be a multiple of 64")
arg_parser.add_argument('-s', '--steps', action="store", required=False, type=int, help="The amount of steps to use for this generation")
arg_parser.add_argument('--api_key', type=str, action='store', required=False, help="The API Key to use to authenticate on the Horde. Get one in https://stablehorde.net")
arg_parser.add_argument('-f', '--filename', type=str, action='store', required=False, help="The filename to use to save the images. If more than 1 image is generated, the number of generation will be prepended")
args = arg_parser.parse_args()


filename = args.filename if args.filename else "horde_generation.png"

imgen_params = {
    "n": args.amount if args.amount else 1,
    "width": args.width if args.width else 64*8,
    "height": args.height if args.height else 64*8,
    "steps": args.steps if args.steps else 50,
    # You can put extra params here if you wish
}

submit_dict = {
    "prompt": args.prompt if args.prompt else "a horde of cute stable robots in a sprawling server room repairing a massive mainframe",
    "api_key": args.api_key if args.api_key else "0000000000",
    "params": imgen_params,
}

@logger.catch
def generate():
    submit_req = requests.post('https://stablehorde.net/api/v1/generate/sync', json = submit_dict)
    if submit_req.ok:
        results = submit_req.json()
        final_filename = filename
        for iter in range(len(results)):
            b64img = results[iter]["img"]
            base64_bytes = b64img.encode('utf-8')
            img_bytes = base64.b64decode(base64_bytes)
            img = Image.frombytes('RGB', (imgen_params["width"],imgen_params["height"]), img_bytes, "raw")
            if len(results) > 1:
                final_filename = f"{iter}_{filename}"
            img.save(final_filename)
    else:
        print(submit_req.text)

generate()