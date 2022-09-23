from flask import Flask, render_template, redirect, url_for, request, Blueprint
from werkzeug.middleware.proxy_fix import ProxyFix

REST_API = Flask(__name__)
REST_API.wsgi_app = ProxyFix(REST_API.wsgi_app, x_for=1)

