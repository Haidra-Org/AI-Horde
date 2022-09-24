from dotenv import load_dotenv
load_dotenv()

import os, logging
from horde import logger, args, HORDE



if __name__ == "__main__":
    # Only setting this for the WSGI logs
    logging.basicConfig(format='%(asctime)s - %(levelname)s - %(module)s:%(lineno)d - %(message)s',level=logging.WARNING)
    from waitress import serve
    logger.init("WSGI Server", status="Starting")
    url_scheme = 'https'
    if args.insecure:
        os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1' # Disable this on prod
        url_scheme = 'http'
    serve(HORDE, host="127.0.0.1", port=args.port, url_scheme=url_scheme, threads=300, connection_limit=4096)
    # HORDE.run(debug=True,host="0.0.0.0",port="5001")
    logger.init("WSGI Server", status="Stopped")
