import os

from flask import Flask
from flask_caching import Cache
from flask_sqlalchemy import SQLAlchemy
from werkzeug.middleware.proxy_fix import ProxyFix

from horde.logger import logger
from horde.redis_ctrl import ger_cache_url, is_redis_up

cache = None
HORDE = Flask(__name__)
HORDE.config.SWAGGER_UI_DOC_EXPANSION = 'list'
HORDE.wsgi_app = ProxyFix(HORDE.wsgi_app, x_for=1)

SQLITE_MODE = os.getenv("USE_SQLITE", "0") == "1"

if SQLITE_MODE:
    logger.warning("Using SQLite for database")
    HORDE.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///horde.db"
else:
    HORDE.config["SQLALCHEMY_DATABASE_URI"] = (
        f"postgresql://{os.getenv('POSTGRES_USER', 'postgres')}:" f"{os.getenv('POSTGRES_PASS')}@{os.getenv('POSTGRES_URL')}"
    )
    HORDE.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "pool_size": 50,
        "max_overflow": -1,
        # "pool_pre_ping": True,
    }
HORDE.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(HORDE)
db.init_app(HORDE)

if not SQLITE_MODE:
    with HORDE.app_context():
        logger.warning(f"pool size = {db.engine.pool.size()}")
logger.init_ok("Horde Database", status="Started")

if is_redis_up():
    try:
        cache_config = {
            "CACHE_REDIS_URL": ger_cache_url(),
            "CACHE_TYPE": "RedisCache",
            "CACHE_DEFAULT_TIMEOUT": 300,
        }
        cache = Cache(config=cache_config)
        cache.init_app(HORDE)
        logger.init_ok("Flask Cache", status="Connected")
    except Exception as e:
        logger.error(f"Flask Cache Failed: {e}")

# Allow local workstation run
if cache is None:
    cache_config = {"CACHE_TYPE": "SimpleCache", "CACHE_DEFAULT_TIMEOUT": 300}
    cache = Cache(config=cache_config)
    cache.init_app(HORDE)
    logger.init_warn("Flask Cache", status="SimpleCache")
