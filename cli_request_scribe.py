import requests, json, os, time, argparse, base64
from cli_logger import logger, set_logger_verbosity, quiesce_logger, test_logger
from PIL import Image
from io import BytesIO
from requests.exceptions import ConnectionError

arg_parser = argparse.ArgumentParser()
arg_parser.add_argument('--api_key', type=str, action='store', required=False, help="The API Key to use to authenticate on the Horde. Get one in https://aihorde.net/register")
arg_parser.add_argument('-n', '--amount', action="store", required=False, type=int, help="The amount of images to generate with this prompt")
arg_parser.add_argument('-p','--prompt', action="store", required=False, type=str, help="The prompt with which to generate images")
arg_parser.add_argument('-c', '--max_context_length', action="store", required=False, type=int, help="The maximum amount of tokens to read from the prompt")
arg_parser.add_argument('-l', '--max_length', action="store", required=False, type=int, help="The maximum amount of tokens to generate")
arg_parser.add_argument('-v', '--verbosity', action='count', default=0, help="The default logging level is ERROR or higher. This value increases the amount of logging seen in your screen")
arg_parser.add_argument('-q', '--quiet', action='count', default=0, help="The default logging level is ERROR or higher. This value decreases the amount of logging seen in your screen")
arg_parser.add_argument('--horde', action="store", required=False, type=str, default="https://aihorde.net", help="Use a different horde")
arg_parser.add_argument('--trusted_workers', action="store_true", default=False, required=False, help="If true, the request will be sent only to trusted workers.")
args = arg_parser.parse_args()


class RequestData(object):
    def __init__(self):
            self.client_agent = "cli_request_scribe.py:1.1.0:(discord)db0#1625"
            self.api_key = "0000000000"
            self.txtgen_params = {
                "n": 1,
                "max_context_length": 1024,
                "max_length": 80,
            }
            self.submit_dict = {
                "prompt": "a horde of cute kobolds furiously typing on typewriters",
                "api_key": "0000000000",
                "trusted_workers": False,
                "models": [],
            }

    def get_submit_dict(self):
        submit_dict = self.submit_dict.copy()
        submit_dict["params"] = self.txtgen_params
        return(submit_dict)
    
def load_request_data():
    request_data = RequestData()
    try:
        import cliRequestsData_Scribe as crd
        try:
            request_data.api_key = crd.api_key
        except AttributeError:
            pass
        try:
            for p in crd.txtgen_params:
                request_data.txtgen_params[p] = crd.txtgen_params[p]
        except AttributeError:
            pass
        try:
            for s in crd.submit_dict:
                request_data.submit_dict[s] = crd.submit_dict[s]
        except AttributeError:
            pass
    except:
        logger.warning("cliRequestData.py could not be loaded. Using defaults with anonymous account")
    if args.api_key: request_data.api_key = args.api_key 
    if args.amount: request_data.txtgen_params["n"] = args.amount 
    if args.max_context_length: request_data.txtgen_params["max_context_length"] = args.max_context_length 
    if args.max_length: request_data.txtgen_params["max_length"] = args.max_length 
    if args.prompt: request_data.submit_dict["prompt"] = args.prompt 
    if args.trusted_workers: request_data.submit_dict["trusted_workers"] = args.trusted_workers 
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
    submit_req = requests.post(f'{args.horde}/api/v2/generate/text/async', json = request_data.get_submit_dict(), headers = headers)
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
                    chk_req = requests.get(f'{args.horde}/api/v2/generate/text/status/{req_id}')
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
            retrieve_req = requests.delete(f'{args.horde}/api/v2/generate/text/status/{req_id}')
        if not cancelled:
            retrieve_req = requests.get(f'{args.horde}/api/v2/generate/text/status/{req_id}')
        if not retrieve_req.ok:
            logger.error(retrieve_req.text)
            return
        results_json = retrieve_req.json()
        # logger.debug(results_json)
        if results_json['faulted']:
            final_submit_dict = request_data.get_submit_dict()
            logger.error(f"Something went wrong when generating the request. Please contact the horde administrator with your request details: {final_submit_dict}")
            return
        results = results_json['generations']
        for iter in range(len(results)):
            logger.generation(f"{iter}: {results[iter]['text']}")
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
            self.txtgen_params = {
                "n": 1,
                "max_context_length": 1024,
                "max_length":80,
            }
            self.submit_dict = {
                "prompt": "a horde of cute kobolds furiously typing on typewriters",
                "api_key": "0000000000",
            }
    crd = temp()


generate()
