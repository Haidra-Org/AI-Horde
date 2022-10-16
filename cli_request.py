import requests, json, os, time, argparse, base64
from cli_logger import logger, set_logger_verbosity, quiesce_logger, test_logger
from PIL import Image, ImageFont, ImageDraw, ImageFilter, ImageOps
from io import BytesIO

arg_parser = argparse.ArgumentParser()
arg_parser.add_argument('-n', '--amount', action="store", required=False, type=int, help="The amount of images to generate with this prompt")
arg_parser.add_argument('-p','--prompt', action="store", required=False, type=str, help="The prompt with which to generate images")
arg_parser.add_argument('-w', '--width', action="store", required=False, type=int, help="The width of the image to generate. Has to be a multiple of 64")
arg_parser.add_argument('-l', '--height', action="store", required=False, type=int, help="The height of the image to generate. Has to be a multiple of 64")
arg_parser.add_argument('-s', '--steps', action="store", required=False, type=int, help="The amount of steps to use for this generation")
arg_parser.add_argument('--api_key', type=str, action='store', required=False, help="The API Key to use to authenticate on the Horde. Get one in https://stablehorde.net")
arg_parser.add_argument('-f', '--filename', type=str, action='store', required=False, help="The filename to use to save the images. If more than 1 image is generated, the number of generation will be prepended")
arg_parser.add_argument('-v', '--verbosity', action='count', default=0, help="The default logging level is ERROR or higher. This value increases the amount of logging seen in your screen")
arg_parser.add_argument('-q', '--quiet', action='count', default=0, help="The default logging level is ERROR or higher. This value decreases the amount of logging seen in your screen")
arg_parser.add_argument('--horde', action="store", required=False, type=str, default="https://stablehorde.net", help="Use a different horde")
arg_parser.add_argument('--nsfw', action="store_true", default=False, required=False, help="Mark the request as NSFW. Only servers which allow NSFW will pick it up")
arg_parser.add_argument('--censor_nsfw', action="store_true", default=False, required=False, help="If the request is SFW, and the worker accidentaly generates NSFW, it will send back a censored image.")
arg_parser.add_argument('--trusted_workers', action="store_true", default=False, required=False, help="If true, the request will be sent only to trusted workers.")
arg_parser.add_argument('--source_image', action="store", required=False, type=str, help="When a file path is provided, will be used as the source for img2img")
args = arg_parser.parse_args()


filename = "horde_generation.png"
api_key = "0000000000"
# You can fill these in to avoid putting them as args all the time
imgen_params = {
    # You can put extra SD webui params here if you wish
}
submit_dict = {
}

@logger.catch
def generate():
    final_filename = args.filename if args.filename else crd.filename
    final_api_key = args.api_key if args.api_key else crd.api_key
    final_imgen_params = {
        "n": args.amount if args.amount else crd.imgen_params.get('n',1),
        "width": args.width if args.width else crd.imgen_params.get('width',512),
        "height": args.height if args.height else crd.imgen_params.get('height',512),
        "steps": args.steps if args.steps else crd.imgen_params.get('steps',50),
    }
    for p in ["denoising_strength", 'sampler_name']:
        if p in crd.imgen_params:
            final_imgen_params[p] = crd.imgen_params[p]

    final_submit_dict = {
        "prompt": args.prompt if args.prompt else crd.submit_dict.get('prompt',"a horde of cute stable robots in a sprawling server room repairing a massive mainframe"),
        "params": final_imgen_params,
        "nsfw": args.nsfw if args.nsfw else crd.submit_dict.get("nsfw", False),
        "censor_nsfw": args.censor_nsfw if args.censor_nsfw else crd.submit_dict.get("censor_nsfw", True),
        "trusted_workers": args.trusted_workers if args.trusted_workers else crd.submit_dict.get("trusted_workers", True),
    }
    final_src_img = args.source_image if args.source_image else crd.source_image
    if final_src_img:
        final_src_img = Image.open(final_src_img)
        buffer = BytesIO()
        # We send as WebP to avoid using all the horde bandwidth
        final_src_img.save(buffer, format="WebP", quality=90)
        final_submit_dict["source_image"] = base64.b64encode(buffer.getvalue()).decode("utf8")
    # final_submit_dict["source_image"] = 'Test'
    headers = {"apikey": final_api_key}
    logger.debug(final_submit_dict)
    submit_req = requests.post(f'{args.horde}/api/v2/generate/async', json = final_submit_dict, headers = headers)
    if submit_req.ok:
        submit_results = submit_req.json()
        logger.debug(submit_results)
        req_id = submit_results['id']
        is_done = False
        while not is_done:
            chk_req = requests.get(f'{args.horde}/api/v2/generate/check/{req_id}')
            if not chk_req.ok:
                logger.error(chk_req.text)
                return
            chk_results = chk_req.json()
            logger.info(chk_results)
            is_done = chk_results['done']
            time.sleep(1)
        retrieve_req = requests.get(f'{args.horde}/api/v2/generate/status/{req_id}')
        if not retrieve_req.ok:
            logger.error(retrieve_req.text)
            return
        results_json = retrieve_req.json()
        # logger.debug(results_json)
        results = results_json['generations']
        for iter in range(len(results)):
            b64img = results[iter]["img"]
            base64_bytes = b64img.encode('utf-8')
            img_bytes = base64.b64decode(base64_bytes)
            img = Image.open(BytesIO(img_bytes))
            if len(results) > 1:
                final_filename = f"{iter}_{filename}"
            img.save(final_filename)
            logger.info(f"Saved {final_filename}")
    else:
        logger.error(submit_req.text)

set_logger_verbosity(args.verbosity)
quiesce_logger(args.quiet)

try:
    import cliRequestsData as crd
    logger.info("Imported cliRequestsData")
except:
    logger.warning("No cliRequestsData found, use default where no CLI args are set")
    class temp(object):
        def __init__(self):
            self.filename = "horde_generation.png"
            self.imgen_params = {
                "n": 1,
                "width": 64*8,
                "height":64*8,
                "steps": 50,
            }
            self.submit_dict = {
                "prompt": "a horde of cute stable robots in a sprawling server room repairing a massive mainframe",
                "api_key": "0000000000",
            }
    crd = temp()


generate()
