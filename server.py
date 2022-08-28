from flask import Flask
from flask_restful import Resource, reqparse, Api
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import socket
import logging
import requests
from enum import Enum

class ServerErrors(Enum):
    INVALIDKAI = 0
    CONNECTIONERR = 1


###Variables goes here###

servers = []

###Code goes here###


REST_API = Flask(__name__)
# Very basic DOS prevention
limiter = Limiter(
    REST_API,
    key_func=get_remote_address,
    default_limits=["90 per minute"]
)
api = Api(REST_API)


def get_error(error, kai_instance):
    if error == ServerErrors.INVALIDKAI:
        logging.warn(f'Invalid KAI instance: {kai_instance}')
        return(f"Server {kai_instance} appears running but does not appear to be a KoboldAI instance. Please start KoboldAI first then try again!")
    if error == ServerErrors.CONNECTIONERR:
        logging.warn(f'Connection Error when attempting to reach server: {kai_instance}')
        return(f"KoboldAI instance {kai_instance} does not seem to be responding. Please load KoboldAI first, ensure it's reachable through the internet, then try again", 400)


@REST_API.after_request
def after_request(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "POST, GET, OPTIONS, PUT, DELETE"
    response.headers["Access-Control-Allow-Headers"] = "Accept, Content-Type, Content-Length, Accept-Encoding, X-CSRF-Token, Authorization"
    return response

class Register(Resource):
    #decorators = [limiter.limit("1/minute")]
    decorators = [limiter.limit("10/minute")]
    def post(self):
        server = ""
        parser = reqparse.RequestParser()
        parser.add_argument("url", type=str, required=True, help="Full URL The KoboldAI server. E.g. 'https://example.com:5000'")
        parser.add_argument("username", type=str, required=True, help="Username for questions")
        args = parser.parse_args()
        kai_instance = args["url"]
        try:
            model_req = requests.get(kai_instance + '/api/latest/model')
            if type(model_req.json()) is not dict:
                return(f"{get_error(ServerErrors.INVALIDKAI,kai_instance)}",400)
            model = model_req.json()["result"]
        except requests.exceptions.JSONDecodeError:
            return(f"{get_error(ServerErrors.INVALIDKAI,kai_instance)}",400)
        except requests.exceptions.ConnectionError:
            return(f"{get_error(ServerErrors.CONNECTIONERR,kai_instance)}",400)
        username = args["username"]
        logging.info(f'{username} adds server {kai_instance}')
        servers.append(kai_instance)
        return("OK",200)

class List(Resource):
    #decorators = [limiter.limit("1/minute")]
    decorators = [limiter.limit("10/minute")]
    def get(self):
        return(servers,200)

if __name__ == "__main__":
    #logging.basicConfig(filename='server.log', encoding='utf-8', level=logging.DEBUG)
    logging.basicConfig(format='%(asctime)s - %(levelname)s - %(module)s:%(lineno)d - %(message)s',level=logging.DEBUG)

    api.add_resource(Register, "/register/")
    api.add_resource(List, "/list/")
    # api.add_resource(Register, "/register")
    from waitress import serve
    #serve(REST_API, host="0.0.0.0", port="5000")
    REST_API.run(debug=True,host="0.0.0.0",port="5001")