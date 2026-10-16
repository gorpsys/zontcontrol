# Контракты приложения

Описывает реализованное локальное приложение API v1 на 16.10.2026.
[Архитектура](architecture.md) раскрывает границы и решения; точные DTO и
ошибки определены в zontcontrol/controller/contracts.py, алгоритмы —
в zontcontrol/autonomy/contracts.py. Прототип удалён без обратной совместимости.

## Слои и входы

controller/auth выполняет авторизацию, controller/transport — HTTP с явным
таймаутом, controller/zont — вызовы ZONT и нормализацию через inventory/history.
controller/service предоставляет потребительский API и ownership; coordination
отвечает за R1/R2 и наблюдения. commands готовит и проверяет команды;
anomaly/monitor/detection_profile сохраняют чистую логику детектора.
autonomy/runtime и registry управляют потоками и состоянием; application
связывает зависимости. algorithms/boiler_reset и boiler_cli независимы.

zontcontrol и python -m zontcontrol вызывают application.main; утилита токена —
zontcontrol-get-token или python -m zontcontrol.get_token. Старых client.py,
cli.py, control.py, ZontClient, apply_command и entrypoint zontcontrol-control нет.
Legacy-адаптеры и re-export не вводить. Исследовательские скрипты используют
create_read_controller без регистратора и координатора.

create_application предоставляет controller(), list_algorithms(mode=None),
list_continuous(), run_once(id, timeout=None), run_all_once(timeouts=None),
start(id), stop(id, wait_timeout=0), host(cancelled=None), close(wait_timeout=None)
и контекстный менеджер. wait_timeout передаётся по имени. CLI: list,
once ID [--timeout SECONDS], all, start ID, host [--start ID]. stop работает
только в том же процессе через Python API; host штатно завершается по
SIGINT/SIGTERM. IPC нет.

## Чтение и время

API чтения: list_devices, describe_values, read_current, read_history,
list_controls. Значения адресуются ValueKey(device_id, kind, element_id,
field), управление — ControlKey(device_id, kind, element_id).
target_temp и setpoint_temp берутся из выбранного контура; скрытый «Котел»
не заменяет «Отопление». Тип consumer не доказывает регулирование по воздуху.
null не равен нулю. Даты timezone-aware; передача и хранение — UTC.

received_at — получение ответа, measured_at — известное время измерения либо
null. Снимок не объявляется атомарным. История сохраняет фактическое покрытие,
пропуски, null и time_truncated; будущие значения и линейная интерполяция
не используются для решения. worktime — запрос автоматики, не доказательство
пламени. Подробные форматы — [справочник](../reference/zont-api.md).

## Детектор и сброс

Независимое решение по последним 45 минутам сохраняет калибровку SMART 2.0.
warning и confirmed_pattern дают anomaly=true, suppressed/normal — false,
unknown — null. Старое событие окна не определяет текущее решение; normal
не гарантирует исправность. boiler_reset действует только при текущем
confirmed_pattern. Дополнительного допуска истории и автоматического
пересчёта из-за ручного вмешательства нет.

«Сброс котла» true отключает котёл, false включает. Модуль делает импульс
40 секунд и немедленно пытается выполнить off в finally при остановке/ошибке.
Уже включённый до импульса сброс не трогает. Continuous после off ждёт пять
минут; once завершается без этой паузы. Политика повторов — самого модуля,
общего guard, хранилища, watchdog и журнала восстановления нет.
[Полный контракт](../reference/boiler-reset.md).

## Команды и жизненный цикл

prepare_command читает актуальные возможности и создаёт ValidatedCommand без
управляющего эффекта; чистая подготовка без сети доступна в commands.
execute_command
требует собственной действующей ControlSession на весь прибор и повторяет
актуальные проверки. try_acquire_control(device_id, lease_seconds=...) и
release_control задают границу монополии. Без неё — ошибка без команды ZONT.
Разные приборы допускают параллельное управление, чтения/preview не блокируются.

Принятая команда завершается независимо от последующего stop/timeout once.
Это относится и к finally. Монополия удерживается до завершения finally и
принятых команд. Новые команды при локальном истечении R2 отклоняются,
включая finally; локальный mutex не продлевает аренду. LeaseLost останавливает
координатора, не отзывает сессию алгоритма. Межклиентская схема и принятый риск —
[лидерство](../reference/leader-election.md), [статистика](../reference/orchestration-risk.md).

У команды три исхода: ответ API, явная ошибка либо CommandOutcomeUnknown
после отправки при timeout/обрыве. Повторов нет; успех API не доказывает
физического восстановления. Обязательного сервиса operations/мониторинга нет.
boiler_cli требует --execute для ручной отправки после preview; автономный
boiler_reset исполняет своё действие без общего execute-флага.

Только once имеет deadline регистратора: TimeoutError выставляет отмену;
continuous владеет внутренним циклом и таймаутами. Остановка кооперативная.
Зависший поток остаётся stopping, удерживает сессию и не перезапускается.
Ограниченное stop/close может закончиться раньше потока; общие ресурсы
доступны до cleanup. Kill/смерть процесса вне гарантии finally.

Прямой ЛК остаётся с принятым человеком риском. Занятость видна в name/R2
с исходным префиксом, но это не техническая блокировка ЛК и не доказательство
авторства команды. Ручной on во время импульса не отменяет его finally off.

## Конфиги и секреты

Файлы относительно cwd: config/controller.json, config/registry.json,
config/\<algorithm_id\>.json. Последний читает и проверяет сама фабрика модуля;
регистратор не знает module_configs. Общих --config/ZONT_CONFIG нет.
Примеры и рабочие конфиги не включаются в wheel. [Схемы](../../config/README.md).

ZONT_TOKEN — только окружение; контакт приложения — controller.json.
Интерактивная утилита токена имеет закрытый ввод пароля. Не выводить секреты
или произвольные тела API-ошибок. Облачный Lockbox и деплой остаются планом.
