import requests, json, os, time, argparse, base64
from cli_logger import logger, set_logger_verbosity, quiesce_logger, test_logger
from PIL import Image
from io import BytesIO
from requests.exceptions import ConnectionError

arg_parser = argparse.ArgumentParser()
arg_parser.add_argument('--api_key', type=str, action='store', required=False, help="The API Key to use to authenticate on the Horde. Get one in https://aihorde.net/register")
arg_parser.add_argument('-f', '--filename', type=str, action='store', required=False, help="The filename to use to save the images. If more than 1 image is generated, the number of generation will be prepended")
arg_parser.add_argument('-v', '--verbosity', action='count', default=0, help="The default logging level is ERROR or higher. This value increases the amount of logging seen in your screen")
arg_parser.add_argument('-q', '--quiet', action='count', default=0, help="The default logging level is ERROR or higher. This value decreases the amount of logging seen in your screen")
arg_parser.add_argument('--horde', action="store", required=False, type=str, default="https://aihorde.net", help="Use a different horde")
arg_parser.add_argument('--trusted_workers', action="store_true", default=False, required=False, help="If true, the request will be sent only to trusted workers.")
arg_parser.add_argument('--source_image', action="store", required=False, type=str, help="When a file path is provided, will be used as the source for img2img")
args = arg_parser.parse_args()


class RequestData(object):
    def __init__(self):
            self.client_agent = "cli_request_alchemy.py:1.0.0:(discord)db0#1625"
            self.api_key = "0000000000"
            self.filename = "horde_alchemy"
            self.submit_dict = {
                "trusted_workers": False,
                "forms": [
                    {"name": "caption"},
                ]
            }
            self.source_image = None

    def get_submit_dict(self):
        submit_dict = self.submit_dict.copy()
        if self.source_image: 
            final_src_img = Image.open(self.source_image)
            buffer = BytesIO()
            # We send as WebP to avoid using all the horde bandwidth
            final_src_img.save(buffer, format="Webp", quality=95, exact=True)
            submit_dict["source_image"] = base64.b64encode(buffer.getvalue()).decode("utf8")
        else:
            logger.error("Alchemy requires a source image.")
            sys.exit(1)
        return(submit_dict)
    
def load_request_data():
    request_data = RequestData()
    try:
        request_data.api_key = crd.api_key
    except AttributeError:
        pass
    try:
        request_data.filename = crd.filename
    except AttributeError:
        pass
    try:
        for p in crd.alchemy_params:
            request_data.alchemy_params[p] = crd.alchemy_params[p]
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
    if args.api_key: request_data.api_key = args.api_key 
    if args.filename: request_data.filename = args.filename 
    if args.trusted_workers: request_data.submit_dict["trusted_workers"] = args.trusted_workers 
    if args.source_image: self.source_image = args.source_image
    return(request_data)


@logger.catch(reraise=True)
def generate():
    request_data = load_request_data()
    # final_submit_dict["source_image"] = 'Test'
    headers = {
        "apikey": request_data.api_key,
        "Client-Agent": request_data.client_agent,
    }
    # logger.debug(request_data.get_submit_dict())
    submit_req = requests.post(f'{args.horde}/api/v2/interrogate/async', json = request_data.get_submit_dict(), headers = headers)
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
                    chk_req = requests.get(f'{args.horde}/api/v2/interrogate/status/{req_id}')
                    if not chk_req.ok:
                        logger.error(chk_req.text)
                        return
                    chk_results = chk_req.json()
                    logger.debug(
                        [
                            {
                                'form': f['form'],
                                'state': f['state'],
                            } for f in chk_results['forms']
                        ]
                    )
                    is_done = chk_results['state'] in ["done", "faulted"]
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
            retrieve_req = requests.delete(f'{args.horde}/api/v2/interrogate/status/{req_id}')
        if not cancelled:
            retrieve_req = requests.get(f'{args.horde}/api/v2/interrogate/status/{req_id}')
        if not retrieve_req.ok:
            logger.error(retrieve_req.text)
            return
        results_json = retrieve_req.json()
        # logger.debug(results_json)
        if results_json['state'] == "faulted":
            final_submit_dict = request_data.get_submit_dict()
            final_submit_dict["source_image"] = f"Alchemy request with size: {len(final_submit_dict['source_image'])}"
            logger.error(f"Something went wrong when generating the request. Please contact the horde administrator with your request details: {final_submit_dict}")
            return
        results = results_json['forms']
        for iter in range(len(results)):
            form = results[iter]['form']
            if results[iter]['state'] == "faulted":
                logger.warning(f"{results[iter]['form']} has faulted")
            elif results[iter]['state'] == "cancelled":
                logger.warning(f"{results[iter]['form']} was cancelled")
            elif form == "interrogation":
                final_filename = f"{request_data.filename}_{form}.txt"
                interrogate = json.dumps(results[iter]['result'][form], indent=4)
                with open(final_filename, 'w') as handler:
                    handler.write(interrogate)
                logger.generation(f"{form} result saved in: {final_filename}")
            elif type(results[iter]['result'][form]) is str and results[iter]['result'][form].startswith("http"):
                final_filename = f"{request_data.filename}_{form}.webp"
                logger.debug(f"Downloading '{form}' from {results[iter]['result'][form]}")
                try:
                    img_data = requests.get(results[iter]['result'][form]).content
                except Exception as err:
                    logger.error(f"Error: {err}")
                with open(final_filename, 'wb') as handler:
                    handler.write(img_data)
                logger.generation(f"{form} result saved in: {final_filename}")
            else:
                logger.generation(f"{form} result: {results[iter]['result'][form]}")
    else:
        logger.error(submit_req.text)

set_logger_verbosity(args.verbosity)
quiesce_logger(args.quiet)

try:
    import cliRequestsData_Alchemy as crd
    logger.info("Imported cliRequestsData_Alchemy.py")
except:
    logger.warning("No cliRequestsData_Alchemy.py found, use default where no CLI args are set")
    class temp(object):
        def __init__(self):
            self.filename = "horde_alchemy.png"
            self.submit_dict = {
                "trusted_workers": False,
                "forms": [
                    {"name": "caption"},
                ]
            }
            self.source_image = './db0.jpg'
    crd = temp()


generate()
