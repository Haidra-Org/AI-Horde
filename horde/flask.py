from flask import Flask
from werkzeug.middleware.proxy_fix import ProxyFix

HORDE = Flask(__name__)
HORDE.wsgi_app = ProxyFix(HORDE.wsgi_app, x_for=1)

