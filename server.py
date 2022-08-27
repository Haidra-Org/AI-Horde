from flask import Flask
from flask_restful import Resource, reqparse, Api
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import socket
import logging

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
        parser.add_argument("server", type=str, required=True, help="The server to connect to")
        parser.add_argument("port", type=int, required=False, default=443, help="The IP of the server to connect to")
        parser.add_argument("username", type=str, required=True, help="Username for questions")
        args = parser.parse_args()
        try:
            server = socket.gethostbyname(args["server"])
        except:
            logging.warn(f'invalid command: {username} - {server}:{port}')
            return(f'Wrong hostname or hostname not found: {server}',500)
        port = args["port"]
        username = args["username"]
        logging.info(f'{username} adds server {server}:{port}')
        if port == 443:
            server = f'https://{server}:{port}'
        else:
            server = f'http://{server}:{port}'
        servers.append(server)
        return("OK",200)

class List(Resource):
    #decorators = [limiter.limit("1/minute")]
    decorators = [limiter.limit("10/minute")]
    def get(self):
        return(servers,200)

if __name__ == "__main__":
    #logging.basicConfig(filename='server.log', encoding='utf-8', level=logging.DEBUG)
    logging.basicConfig(format='%(asctime)s - %(levelname)s - %(module)s:%(lineno)d - %(message)s',level=logging.DEBUG)
    
    api.add_resource(Register, "/register")
    api.add_resource(Register, "/register")
    from waitress import serve
    #serve(REST_API, host="0.0.0.0", port="5000")
    REST_API.run(debug=True,host="0.0.0.0",port="5000")