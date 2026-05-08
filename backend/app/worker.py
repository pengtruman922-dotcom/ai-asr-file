from redis import Redis
from rq import Queue, Worker

from .config import get_settings


def main():
    settings = get_settings()
    redis = Redis.from_url(settings.redis_url)
    worker = Worker([Queue("default", connection=redis)], connection=redis)
    worker.work()


if __name__ == "__main__":
    main()
