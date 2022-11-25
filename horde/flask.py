from flask import Flask
from flask_caching import Cache
from werkzeug.middleware.proxy_fix import ProxyFix
from flask_sqlalchemy import SQLAlchemy
from horde.redis_ctrl import is_redis_up, ger_limiter_url
from horde import logger


cache = None
HORDE = Flask(__name__)
HORDE.wsgi_app = ProxyFix(HORDE.wsgi_app, x_for=1)
HORDE.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///horde.db"
db = SQLAlchemy(HORDE)
db.init_app(HORDE)
db.create_all()
logger.init_ok("Horde Database", status="Started")

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
