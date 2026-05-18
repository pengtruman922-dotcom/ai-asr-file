import sys

from redis import Redis
from rq import Queue, Worker

from .config import get_settings


QUEUE_ALIASES = {
    "split": ["asr", "llm", "extract", "default"],
    "all": ["asr", "llm", "extract", "default"],
}


def resolve_queue_names(argv: list[str], configured: str) -> list[str]:
    raw_values = argv or ([configured] if configured else [])
    names: list[str] = []
    for raw_value in raw_values:
        for item in str(raw_value).split(","):
            queue_name = item.strip()
            if not queue_name:
                continue
            names.extend(QUEUE_ALIASES.get(queue_name, [queue_name]))
    if not names:
        names = ["default"]

    unique_names: list[str] = []
    for name in names:
        if name not in unique_names:
            unique_names.append(name)
    return unique_names


def main():
    settings = get_settings()
    redis = Redis.from_url(settings.redis_url)
    queue_names = resolve_queue_names(sys.argv[1:], settings.worker_queues)
    print(f"Starting RQ worker for queues: {', '.join(queue_names)}", flush=True)
    worker = Worker([Queue(name, connection=redis) for name in queue_names], connection=redis)
    worker.work(with_scheduler=True)


if __name__ == "__main__":
    main()
