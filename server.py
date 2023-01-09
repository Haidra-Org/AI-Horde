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
    if args.horde == "stable":
        allowed_host = "stablehorde.net"
    else:
        allowed_host = "127.0.0.1"
    if args.insecure:
        allowed_host = "0.0.0.0"
        logger.init_warn("WSGI Mode", status="Insecure")
    serve(HORDE, host=allowed_host, port=args.port, url_scheme=url_scheme, threads=1000, connection_limit=8192, asyncore_use_poll=True)
    # HORDE.run(debug=True,host="0.0.0.0",port="5001")
    logger.init("WSGI Server", status="Stopped")
