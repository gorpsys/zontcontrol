"""One-shot sensor reading with the token from the process environment."""

import argparse
import json
import os
import sys

from .client import ZontClient, ZontError
from .history import cli_period
from .monitor import assess_history
from . import __version__


def main() -> int:
    parser = argparse.ArgumentParser(description="Прочитать датчики через ZONT API v3")
    parser.add_argument("--version", action="version", version=f"zontcontrol {__version__}")
    parser.add_argument("--contact", help="Контактный email для X-ZONT-Client")
    parser.add_argument("--device-id", type=int, help="Прочитать только указанный прибор")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--list-controls", action="store_true", help="Список элементов управления и команд API")
    mode.add_argument("--history", action="store_true", help="История температур и контуров (по умолчанию 24 часа)")
    mode.add_argument("--detect-anomalies", action="store_true", help="Проверить отсутствие теплового отклика, без управления")
    parser.add_argument("--hours", type=float, help="Глубина истории в часах")
    parser.add_argument("--from", dest="start", help="Начало истории в RFC 3339 с часовым поясом")
    parser.add_argument("--to", dest="end", help="Конец истории в RFC 3339 с часовым поясом")
    parser.add_argument("--circuit-id", type=int, help="Один контур при получении истории")
    parser.add_argument("--include-controls", action="store_true", help="Добавить историю переключателей к --history")
    parser.add_argument("--timeout", type=float, default=15, help="Таймаут в секундах (15)")
    args = parser.parse_args()
    if not (args.history or args.detect_anomalies) and (args.include_controls or any(value is not None for value in (args.hours, args.start, args.end, args.circuit_id))):
        parser.error("Параметры периода и --include-controls требуют --history или --detect-anomalies")
    if args.detect_anomalies and args.circuit_id is None:
        parser.error("--detect-anomalies требует --circuit-id")
    token = os.environ.get("ZONT_TOKEN")
    if token is None or not token.strip():
        print(
            "Не задана переменная окружения ZONT_TOKEN.\n"
            "Получите токен: python3 -m zontcontrol.get_token --contact YOUR_EMAIL\n"
            "Утилита запросит логин и пароль ZONT и выведет токен.\n"
            "Затем задайте переменную: export ZONT_TOKEN='полученный_токен'\n"
            "И повторите запуск: python3 -m zontcontrol --contact YOUR_EMAIL",
            file=sys.stderr,
        )
        return 1
    try:
        hours = 0.75 if args.detect_anomalies and args.hours is None and args.start is None else args.hours
        period = cli_period(args.start, args.end, hours) if (args.history or args.detect_anomalies) else None
        contact = args.contact or os.environ.get("ZONT_CONTACT")
        if not contact:
            contact = input("Контактный email для API: ").strip()
        client = ZontClient(contact, token=token, timeout=args.timeout)
        if args.history or args.detect_anomalies:
            options = {"device_id": args.device_id, "circuit_id": args.circuit_id}
            if args.include_controls or args.detect_anomalies:
                options["include_controls"] = True
            snapshot = client.read_history(*period, **options)
            if args.detect_anomalies:
                snapshot = assess_history(snapshot, circuit_id=args.circuit_id)
        elif args.list_controls:
            snapshot = client.list_controls(args.device_id)
        else:
            snapshot = client.read_sensors(args.device_id)
        print(json.dumps(snapshot, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except (ZontError, ValueError) as error:
        print(f"Ошибка: {error}", file=sys.stderr)
    except (EOFError, KeyboardInterrupt):
        print("Ввод отменён.", file=sys.stderr)
    return 1
