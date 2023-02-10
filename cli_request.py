import requests, json, os, time, argparse, base64
from cli_logger import logger, set_logger_verbosity, quiesce_logger, test_logger
from PIL import Image, ImageFont, ImageDraw, ImageFilter, ImageOps
from io import BytesIO
from requests.exceptions import ConnectionError

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
arg_parser.add_argument('--source_processing', action="store", required=False, type=str, help="Can either be img2img, inpainting, or outpainting")
arg_parser.add_argument('--source_mask', action="store", required=False, type=str, help="When a file path is provided, will be used as the mask source for inpainting/outpainting")
args = arg_parser.parse_args()


class RequestData(object):
    def __init__(self):
            self.api_key = "0000000000"
            self.filename = "horde_generation.png"
            self.imgen_params = {
                "n": 1,
                "width": 64*8,
                "height":64*8,
                "steps": 50,
                "sampler_name": "k_euler",
                "cfg_scale": 7.5,
                "denoising_strength": 0.6,
            }
            self.submit_dict = {
                "prompt": "a horde of cute stable robots in a sprawling server room repairing a massive mainframe",
                "api_key": "0000000000",
                "nsfw": False,
                "censor_nsfw": False,
                "trusted_workers": False,
                "models": ["stable_diffusion"],
                "r2": True
            }
            self.source_image = None
            self.source_processing = "img2img"
            self.source_mask = None

    def get_submit_dict(self):
        submit_dict = self.submit_dict.copy()
        submit_dict["params"] = self.imgen_params
        submit_dict["source_processing"] = self.source_processing
        if self.source_image: 
            final_src_img = Image.open(self.source_image)
            buffer = BytesIO()
            # We send as WebP to avoid using all the horde bandwidth
            final_src_img.save(buffer, format="Webp", quality=95)
            submit_dict["source_image"] = base64.b64encode(buffer.getvalue()).decode("utf8")
        if self.source_mask: 
            final_src_mask = Image.open(self.source_mask)
            buffer = BytesIO()
            # We send as WebP to avoid using all the horde bandwidth
            final_src_mask.save(buffer, format="Webp", quality=95)
            submit_dict["source_mask"] = base64.b64encode(buffer.getvalue()).decode("utf8")
        return(submit_dict)
    
def load_request_data():
    request_data = RequestData()
    try:
        import cliRequestsData as crd
        try:
            request_data.api_key = crd.api_key
        except AttributeError:
            pass
        try:
            request_data.filename = crd.filename
        except AttributeError:
            pass
        try:
            for p in crd.imgen_params:
                request_data.imgen_params[p] = crd.imgen_params[p]
        except AttributeError:
            pass
        try:
            for s in crd.submit_dict:
                request_data.submit_dict[s] = crd.submit_dict[s]
        except AttributeError:
            pass
        try:
            request_data.source_image = crd.source_image
        except AttributeError:
            pass
        try:
            request_data.source_processing = crd.source_processing
        except AttributeError:
            pass
        try:
            request_data.source_mask = crd.source_mask
        except AttributeError:
            pass
    except:
        logger.warning("cliRequestData.py could not be loaded. Using defaults with anonymous account")
    if args.api_key: request_data.api_key = args.api_key 
    if args.filename: request_data.filename = args.filename 
    if args.amount: request_data.imgen_params["n"] = args.amount 
    if args.width: request_data.imgen_params["width"] = args.width 
    if args.height: request_data.imgen_params["height"] = args.height 
    if args.steps: request_data.imgen_params["steps"] = args.steps 
    if args.prompt: request_data.submit_dict["prompt"] = args.prompt 
    if args.nsfw: request_data.submit_dict["nsfw"] = args.nsfw 
    if args.censor_nsfw: request_data.submit_dict["censor_nsfw"] = args.censor_nsfw 
    if args.trusted_workers: request_data.submit_dict["trusted_workers"] = args.trusted_workers 
    if args.source_image: self.source_image = args.source_image
    if args.source_processing: self.source_processing = args.source_processing
    if args.source_mask: self.source_mask = args.source_mask
    return(request_data)


@logger.catch(reraise=True)
def generate():
    request_data = load_request_data()
    # final_submit_dict["source_image"] = 'Test'
    headers = {"apikey": request_data.api_key}
    # logger.debug(request_data.get_submit_dict())
    submit_req = requests.post(f'{args.horde}/api/v2/generate/async', json = request_data.get_submit_dict(), headers = headers)
    if submit_req.ok:
        submit_results = submit_req.json()
        logger.debug(submit_results)
        req_id = submit_results['id']
        is_done = False
        retry = 0
        cancelled = False
        try:
            while not is_done:
                try:
                    chk_req = requests.get(f'{args.horde}/api/v2/generate/check/{req_id}')
                    if not chk_req.ok:
                        logger.error(chk_req.text)
                        return
                    chk_results = chk_req.json()
                    logger.info(chk_results)
                    is_done = chk_results['done']
                    time.sleep(0.8)
                except ConnectionError as e:
                    retry += 1
                    logger.error(f"Error {e} when retrieving status. Retry {retry}/10")
                    if retry < 10:
                        time.sleep(1)
                        continue
                    raise
        except KeyboardInterrupt:
            logger.info(f"Cancelling {req_id}...")
            cancelled = True
            retrieve_req = requests.delete(f'{args.horde}/api/v2/generate/status/{req_id}')
        if not cancelled:
            retrieve_req = requests.get(f'{args.horde}/api/v2/generate/status/{req_id}')
        if not retrieve_req.ok:
            logger.error(retrieve_req.text)
            return
        results_json = retrieve_req.json()
        # logger.debug(results_json)
        if results_json['faulted']:
            final_submit_dict = request_data.get_submit_dict()
            if "source_image" in final_submit_dict:
                final_submit_dict["source_image"] = f"img2img request with size: {len(final_submit_dict['source_image'])}"
            logger.error(f"Something went wrong when generating the request. Please contact the horde administrator with your request details: {final_submit_dict}")
            return
        results = results_json['generations']
        for iter in range(len(results)):
            final_filename = request_data.filename
            if len(results) > 1:
                final_filename = f"{iter}_{request_data.filename}"
            if request_data.get_submit_dict()["r2"]:
                logger.debug(f"Downloading '{results[iter]['id']}' from {results[iter]['img']}")
                try:
                    img_data = requests.get(results[iter]["img"]).content
                except:
                    logger.error("Received b64 again")
                with open(final_filename, 'wb') as handler:
                    handler.write(img_data)
            else:
                b64img = results[iter]["img"]
                base64_bytes = b64img.encode('utf-8')
                img_bytes = base64.b64decode(base64_bytes)
                img = Image.open(BytesIO(img_bytes))
                img.save(final_filename)
            censored = ''
            if results[iter]["censored"]:
                censored = " (censored)"
            logger.info(f"Saved{censored} {final_filename}")
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
