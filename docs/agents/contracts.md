# Текущие контракты приложения

Controller API_VERSION=2, подготовленный выпуск 0.4.0.
Удаление API монополии — намеренная несовместимость; заглушек прежних
session/request/acquire/release методов нет.
[Архитектура](architecture.md), [решение](../history/2026-10-20-remove-synchronization.md).

## Управляющий API

Один Controller представляет авторизованное ЛК со всеми приборами.
Токен, HTTP и формы двух API ZONT скрыты от потребителей.

| Метод | Результат |
| --- | --- |
| list_devices() | tuple[DeviceInfo, ...]: ID, имя, модель, online |
| describe_values(device_id) | Доступные значения, источник, единицы, current/history |
| read_current(device_id, value_keys=None) | Snapshot |
| read_history(device_id, value_keys, start, end) | HistoryResult с качеством и покрытием |
| list_controls(device_id) | Действия и ограничения всех элементов управления |
| prepare_command(device_id, control_key, action, arguments) | ValidatedCommand для preview |
| execute_command(command) | CommandResult после повторной проверки и одной отправки |

Ключ значения — ValueKey(device_id, kind, element_id, field), управления —
ControlKey(device_id, kind, element_id). Нет неявного первого прибора.
DTO frozen; вложенные коллекции — tuple, значения — scalar.
Метки времени aware UTC; неизвестное measured_at остаётся None.
Snapshot не обещает атомарности; история сохраняет gaps/null/time_truncated
и фактическое покрытие. По умолчанию история разбивается по суткам, максимум
31 часть; некорректный/будущий интервал отклоняется до архивных запросов.

prepare_command не отправляет команду. execute_command проверяет online,
текущие capabilities, диапазоны/шаг и аргументы ещё раз: preview мог устареть.
Локальной или распределённой сериализации нет; параллельные команды одному
прибору допускаются. Name/notes не используются как служебные регистры.

Ошибки: ControllerError, AuthenticationError, DeviceUnavailable,
UnsupportedAction, InvalidArguments, IncompleteHistory, RateLimited,
CommandOutcomeUnknown. Они не принимают произвольный текст upstream.
CommandOutcomeUnknown означает неизвестный исход отправленной команды;
автоповтора нет. CommandResult.api_accepted не доказывает физический результат.
ReadControllerService/create_read_controller не предоставляют execute_command.

## Предметный вход отопления

zontcontrol.heating задаёт frozen HeatingPoint(measured_at, value, quality),
HeatingSeries(points, source, unit, value_type, kind, incomplete_reasons) и
HeatingHistory. Коллекции — tuple, даты aware UTC, значения scalar; исходная
точность дат и value плохого качества сохраняются. Ряды упорядочены по точным
датам, равные даты допустимы; monitor выбирает последний дубль до проверки
качества. Структурные ошибки дают ValueError до детекции.

HeatingHistory содержит явные положительные device_id/circuit_id/
air_sensor_id/coolant_sensor_id/reset_control_id, start/end/received_at,
семь рядов air/coolant/air_target/coolant_target/worktime/target_sensor_id/reset,
time_truncated и incomplete_reasons. Пропуск ряда — None, существующий пустой
ряд — HeatingSeries(points=()). Точки до start допустимы с исходным возрастом.

from_controller_history(history, *, device_id, circuit_id, air_id, coolant_id,
reset_id) из heating_adapter выбирает точные ValueKey и проверяет device_id.
Он сохраняет quality/metadata/неполноту без ISO/JSON и зависимости от конфига.
Вычисления используют Controller value только при quality='good'. Архивный
from_archive_packet с теми же явными ID находится только в scripts/heating_archive;
его quality='good' сохраняет прежнюю вычислительную проекцию, не доказывает
исходное качество. Старые потери load_series не восстанавливаются.

monitor.history_samples(history: HeatingHistory) выдаёт HeatingSample;
monitor.assess_history(history: HeatingHistory, *, config=None) возвращает
прежний dict результата. Словарного входа и старых kwargs ID больше нет.
Каждый вызов создаёт новый NoHeatDetector. Правила сетки и причинной доступности —
[справочник](../reference/anomaly-detection.md#2-пригодность-данных).
requested_period описывает исходный запрос 46 минут; границы расчётной сетки
не добавляются в production-ответ. Неполнота старого участка сама по себе
не запрещает решение после накопления пригодных данных.

## Автономные алгоритмы

AlgorithmSpec содержит algorithm_id, описание, factory, modes,
required_capabilities и minimum_seconds. Capabilities совпадают с семью
методами Controller. Конструктор читает конфиг и проверяет зависимости,
но не запускает потоки/сеть. DI передаёт один общий контроллер.

OnceAlgorithm.run_once(context) возвращает AlgorithmResult;
ContinuousAlgorithm.run_continuous(context) сам организует постоянную работу.
RunContext содержит algorithm_id, cancelled и deadline.
Для once deadline фиксирован при запуске; continuous получает None.
context.wait прерывается отменой. Каждый запуск получает отдельный контекст
и Event; контроллер общий и передаётся фабрике модуля напрямую.

Регистратор выдаёт список модулей/continuous, run_once/run_all_once,
start/stop/close. Once-таймаут публикуется независимо от завершения HTTP
или finally; worker остаётся stopping до выхода. Повторный старт того же
алгоритма в это время запрещён. Один модуль в continuous исключает его once;
другие модули могут управлять тем же прибором одновременно.

У dual-mode continuous по умолчанию не стартует автоматически;
autostart_dual_mode включает это поведение. Continuous-only стартует сразу
после успешной регистрации, если модуль не отключён.
Результаты SUCCESS/UNKNOWN/SUPPRESSED принадлежат алгоритму; unknown не normal.
Ошибки жизненного цикла — AlgorithmBusy/Timeout/Cancelled/Failed.

## Завершение

Application владеет регистратором и транспортом. close запрещает новые старты,
отменяет и ждёт зарегистрированные алгоритмы, включая их finally, потом
закрывает транспорт ровно один раз. Ограниченное ожидание возвращает False
без закрытия ресурсов. Несколько close ждут один shutdown.
Внешние вызовы контроллера завершаются вызывающей стороной до app.close().
Отмена запуска не отменяет уже отправленный HTTP и не запрещает finally off.
Команды имеют транспортный таймаут; принудительной остановки потоков нет.

## Конфиги и секреты

config/controller.json: contact и http_timeout_seconds.
config/registry.json: disabled_modules, autostart_dual_mode, once_timeouts.
config/<algorithm_id>.json читает сам модуль; схемы — [config/README](../../config/README.md).
Старые поля coordination/statistics/lease/grant удалены, загрузчик не игнорирует
их молча. Рабочие файлы в gitignore, шаблоны не содержат токенов.
Токен — окружение ZONT_TOKEN; отдельная get_token утилита скрывает ввод пароля.
