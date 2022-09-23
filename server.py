from dotenv import load_dotenv
load_dotenv()

from flask import Flask, render_template, redirect, url_for, request, Blueprint
from flask_restx import Resource, reqparse, fields, Api, abort
from werkzeug.middleware.proxy_fix import ProxyFix
import requests, random, time, os, oauthlib, secrets, logging
from app import logger, args, REST_API
# from uuid import uuid4
# from app import REST_API, limiter, args
# from app.classes import db as _db



if __name__ == "__main__":
    # Only setting this for the WSGI logs
    logging.basicConfig(format='%(asctime)s - %(levelname)s - %(module)s:%(lineno)d - %(message)s',level=logging.WARNING)
    from waitress import serve
    logger.init("WSGI Server", status="Starting")
    url_scheme = 'https'
    if args.insecure:
        os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1' # Disable this on prod
        url_scheme = 'http'
    serve(REST_API, host="127.0.0.1", port=args.port, url_scheme=url_scheme, threads=4)
    # REST_API.run(debug=True,host="0.0.0.0",port="5001")
    logger.init("WSGI Server", status="Stopped")
