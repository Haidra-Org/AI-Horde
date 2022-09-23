from flask import Flask
from werkzeug.middleware.proxy_fix import ProxyFix

REST_API = Flask(__name__)
REST_API.wsgi_app = ProxyFix(REST_API.wsgi_app, x_for=1)

