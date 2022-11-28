import os
from flask import Flask
from flask_caching import Cache
from werkzeug.middleware.proxy_fix import ProxyFix
from flask_sqlalchemy import SQLAlchemy
from horde.redis_ctrl import is_redis_up
from horde.logger import logger

cache = None
HORDE = Flask(__name__)
HORDE.wsgi_app = ProxyFix(HORDE.wsgi_app, x_for=1)
# HORDE.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///horde.db"
wtf = os.getenv('POSTGRES_PASS')
logger.debug(wtf)
HORDE.config["SQLALCHEMY_DATABASE_URI"] = f"postgresql://postgres:{os.getenv('POSTGRES_PASS')}@localhost/postgres"
HORDE.config['SQLALCHEMY_ENGINE_OPTIONS'] = {"pool_size": 500}
HORDE.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(HORDE)
db.init_app(HORDE)
with HORDE.app_context():
    logger.error("pool size = {}".format(db.engine.pool.size()))
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
    except Exception as e:
        logger.error(f"Flask Cache Failed: {e}")
        pass

# Allow local workatation run
if cache is None:
    cache_config = {
        "CACHE_TYPE": "SimpleCache",
        "CACHE_DEFAULT_TIMEOUT": 300
    }
    cache = Cache(config=cache_config)
    cache.init_app(HORDE)
    logger.init_warn("Flask Cache", status="SimpleCache")
