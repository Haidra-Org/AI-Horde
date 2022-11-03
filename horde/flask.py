from flask import Flask
from flask_caching import Cache
from werkzeug.middleware.proxy_fix import ProxyFix
from .redis_ctrl import is_redis_up, ger_limiter_url
from . import logger


cache = None
HORDE = Flask(__name__)
HORDE.wsgi_app = ProxyFix(HORDE.wsgi_app, x_for=1)

if is_redis_up():
    try:
        cache_config = {
            "CACHE_TYPE": "RedisCache",  
            "CACHE_DEFAULT_TIMEOUT": 300
        }
        cache = Cache(config=cache_config)
        cache.init_app(HORDE)
        logger.init_ok("Flask Cache", status="Connected")
    except:
        pass

# Allow local workatation run
if cache == None:
    cache_config = {
        "CACHE_TYPE": "SimpleCache",
        "CACHE_DEFAULT_TIMEOUT": 300
    }
    cache = Cache(config=cache_config)
    cache.init_app(HORDE)
    logger.init_warn("Flask Cache", status="SimpleCache")
