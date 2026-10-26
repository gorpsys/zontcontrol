# Текущие контракты приложения

Controller API_VERSION=2.
Удаление API монополии — намеренная несовместимость; заглушек прежних
session/request/acquire/release методов нет.
[Архитектура](architecture.md), [решение](../history/2026-10-20-remove-synchronization.md).

## Управляющий API

Один Controller представляет авторизованный ЛК со всеми приборами.
Токен, HTTP и формы двух API ZONT скрыты от потребителей.

| Метод | Результат |
| --- | --- |
| list_devices() | tuple[DeviceInfo, ...]: ID, имя, модель, online |
| read_notes(device_id) | str: точный текст notes; null/отсутствующее значение — пустая строка |
| write_notes(device_id, notes) | None: полная замена notes одной отправкой |
| describe_values(device_id) | Доступные значения, источник, единицы, current/history |
| read_current(device_id, value_keys=None) | Snapshot |
| read_history(device_id, value_keys, start, end) | HistoryResult с качеством и покрытием |
| list_controls(device_id) | Действия и ограничения всех элементов управления |
| prepare_command(device_id, control_key, action, arguments) | ValidatedCommand для preview |
| execute_command(command) | CommandResult после повторной проверки и одной отправки |

Ключ значения — ValueKey(device_id, kind, element_id, field), управления —
ControlKey(device_id, kind, element_id). Нет неявного первого прибора.
Поля DTO нельзя изменять (frozen=True); вложенные коллекции — tuple,
значения — скалярные. Метки времени содержат часовой пояс UTC; неизвестное measured_at остаётся None.
Snapshot не обещает атомарности; история сохраняет gaps/null/time_truncated
и фактическое покрытие. По умолчанию история разбивается по суткам, максимум
31 часть; некорректный/будущий интервал отклоняется до архивных запросов.
Для step допустима одна опорная точка до start с исходным временем и качеством;
она задаёт последнее известное состояние на левой границе. Measurement остаётся
внутри интервала. Null и конфликты опоры не заменяются более старым значением.
Нормализация circuit/worktime и circuit/setpoint_temp одного исходного ответа
выбирает абсолютную метку перед дельтой на том же времени; вид метки остаётся
внутри нормализации.
Противоречия равного приоритета, других полей и разных частей сохраняют unknown —
[правила истории](../reference/zont-api.md#история). Методы и DTO не расширяются.

prepare_command не отправляет команду. execute_command проверяет online,
текущие capabilities, диапазоны/шаг и аргументы ещё раз: preview мог устареть.
Локальной или распределённой сериализации нет; параллельные команды одному
прибору допускаются. Name/notes не используются как служебные регистры.
Для каждого прибора экземпляр ZontAdapter при первом успешном адресном чтении
его метаданных один раз пытается записать name: прежний точный префикс и
` [zontcontrol connected <ISO UTC seconds>]`. Только наш прежний суффикс
заменяется; чужие суффиксы и исходные пробелы сохраняются без обрезания.
list_devices() без ID и создание приложения не переименовывают приборы.
При ошибке остаётся безопасная диагностика без повтора записи, обычная работа
продолжается. Содержимое текущего чтения не подменяется предположением об успехе.
Это побочное действие с метаданными есть и у create_read_controller,
хотя в его интерфейсе нет методов отправки управляющих команд. Отметка не даёт права управления
и при конкурентном переименовании может быть перезаписана другим клиентом.

Ошибки: ControllerError, AuthenticationError, DeviceUnavailable,
UnsupportedAction, InvalidArguments, IncompleteHistory, RateLimited,
CommandOutcomeUnknown. Они не принимают произвольный текст upstream.
CommandOutcomeUnknown означает неизвестный исход отправленной команды;
автоповтора нет. CommandResult.api_accepted не доказывает физический результат.
read_notes и write_notes работают с метаданными ЛК, включая offline-прибор:
они не отправляют физических команд и не требуют online. device_id — явный
положительный int (bool недопустим), notes — str, включая пустую строку для
очистки. Другие поля устройства не изменяются. Некорректные типы отклоняются
до HTTP; неизвестный исход записи не повторяется.
ReadControllerService/create_read_controller предоставляют read_notes, но не
предоставляют execute_command или write_notes.
create_controller — контекстный менеджер полного Controller без регистратора
и алгоритмов: `with create_controller() as controller:`. Он читает только
config/controller.json и ZONT_TOKEN, создаёт собственный транспорт и закрывает
его при выходе. Вызовы завершают внутри этого блока. Чтение notes также
отмечает первое адресное подключение в name; write_notes меняет только notes.

## Предметный вход отопления

zontcontrol.heating задаёт frozen HeatingPoint(measured_at, value, quality),
HeatingSeries(points, source, unit, value_type, kind, incomplete_reasons) и
HeatingHistory. Коллекции — tuple, даты содержат часовой пояс UTC, значения — скалярные; исходная
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
В BoilerReset предметный вход собирается внутри алгоритма из двух отдельно
преобразованных ответов с общим T: телеметрия 46 минут и reset 120 минут.
Сохраняются основное окно, вся reset-серия и метаданные обоих ответов —
[правила детектора](../reference/anomaly-detection.md).
requested_period описывает основной запрос 46 минут; границы расчётной сетки
не добавляются в production-ответ. Неполнота старого участка сама по себе
не запрещает решение после накопления пригодных данных.

## Автономные алгоритмы

AlgorithmSpec содержит algorithm_id, описание, factory, modes,
required_capabilities и minimum_seconds. Capabilities соответствуют девяти
публичным методам Controller, включая read_notes/write_notes.
Конструктор читает конфиг и проверяет зависимости,
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

## Облачный вход

Корневой handler.py экспортирует zontcontrol.cloud.handler(event, context).
Импорт не читает конфиги/токен, не создаёт Application и не загружает CLI.
При каждом вызове настраивается JSON logger, создаётся новый обычный Application
с единственным BOILER_RESET_SPEC, ограниченным Mode.ONCE, и выполняется
ровно один run_once('boiler_reset', timeout=300). В finally вызывается close()
без ограничения ожидания: worker и его off/finally завершаются до закрытия
транспорта и возврата ответа, включая таймаут/отмену результата.

Event игнорируется: он не задаёт устройство, алгоритм, режим или управление.
Фиксированные config/controller.json, registry.json и boiler_reset.json проходят
существующую валидацию до сетевой работы. Исходный профиль —
deploy/cloud-config; пустой contact в controller.json предназначен для будущей
подстановки сборщиком из непустого ZONT_CONTACT. Рабочие локальные конфиги
для облака не используются. Runtime получает ZONT_TOKEN из Lockbox через env.

Ответ содержит version, algorithm_id='boiler_reset', status,
state_at_period_end, reset_performed, reason, assessment и log. status сохраняет
success/unknown/suppressed; reason — null, cancelled, already_on или
current_reset_unknown. Успешный off API не означает физического восстановления.
Данные результата, включая assessment и дополнительные поля, передаются
из AlgorithmResult.value целиком. Это JSON-совместимые данные, подготовленные
самим алгоритмом, без конфигов, credentials, upstream payload и исключений.
Handler не разбирает события, состояния или причины алгоритма.

Основное событие once_finished сохраняет компактную диагностику assessment
и reason алгоритма: период, причины, пригодность и буфер, возраст измерений,
исторический и текущий reset, отдельные флаги усечения двух ответов.
Смысл полей задан в [итоге разового запроса](../reference/anomaly-detection.md#8-итог-разового-запроса).
Форматтер разрешает только эти структурные поля с проверкой типов;
events, исходные ряды и произвольные вложенные данные в событие не копируются.

log — строка с копией Python stdout и stderr за текущий вызов, включая
освобождение ресурсов. Tee передаёт исходный текст в исходный поток и общий буфер; отдельной
проекции для ответа нет. Облачный formatter по-прежнему выдаёт JSONL с timestamp
UTC из LogRecord, чужой formatter сохраняется буквально. Поэтому общий log
может содержать и JSONL, и обычный текст. Имеющиеся StreamHandler с направлением
в stdout/stderr временно перенаправляются в tee, в том числе обработчики корневого и отдельных
логгеров с propagate=False. Не меняются их уровни/форматы/состав, нет добавления
root handler и дублированных записей. В finally восстанавливаются sys.stdout,
sys.stderr и потоки обработчиков логов, включая созданные во время вызова; буфер
закрывается. Тёплые вызовы не делят буфер. Вне вызова накопления нет.
Прямой вывод через файловые дескрипторы ОС или нативный код, файловые обработчики
логов и платформенные START/END вне Python-процесса не захватываются. Секреты запрещено выводить в самом источнике;
накопитель не фильтрует уже выведенные записи.

Сбой, включая off или закрытие транспорта, после cleanup даёт RuntimeError
с JSON-сообщением: version, algorithm_id, status='error', поле error с фиксированной категорией
(timeout/cancelled/configuration_error/execution_failed), result (завершённый
результат или null) и log. Код не пишет текст upstream и chained traceback;
ошибка и принятые повторы платформы сохраняются. Безопасный request_id берётся
из context только для логов; event не выводится.

Перед закрытием основного приложения handler ждёт worker через
stop('boiler_reset', wait_timeout=None) и читает прежние notes через его
Controller. device_id берёт из load_config алгоритма, включая случаи сбоя
до результата; event и содержимое лога не определяют устройство для записи. После полного
основного cleanup, close и конечного cloud-сообщения строка log фиксируется.
Далее отдельный create_controller без регистратора/алгоритмов отправляет
ровно одну запись notes. Эта служебная публикация не пишет собственных
success-логов и не является частью сохраняемого основного лога.

notes — JSON-массив строк, newest-first, максимум три запуска. Каждый элемент
буквально равен log ответа соответствующего вызова, включая Unicode, переводы
строк, stdout/stderr и ошибки основного cleanup. Пустое поле начинает историю;
невалидный JSON, не массив или нестроковый элемент не затираются. Нет лимита
с обрезанием текста: отказ API сохранит полный log в ответе и даст notes_error.
Конкурентная read→write ротация не атомарна: другой запуск/клиент может потерять
запись. Координация, fencing и автоматические повторы не добавляются.

В успешном ответе и JSON ошибки есть notes_saved и notes_error. notes_saved=True
означает подтверждённую API запись, включая последующий сбой закрытия её
транспорта. notes_error — null либо фиксированная категория unavailable,
read_failed, invalid_history, publication_unavailable, write_failed,
write_unknown, publication_cleanup_failed. Без доступной цели/истории запись
не предпринимается. Сбой публикации не меняет основной исход алгоритма и не
инициирует его повтор; неизвестная запись не повторяется. Ошибки основного
вызова по-прежнему остаются исключением для платформы.

Адреса ресурсов и pinned версия секрета с датой подтверждения —
[реестр](../reference/yandex-cloud-resources.md). Версии деплоя,
наблюдённые вызовы и границы внешней проверки —
[отчёт](../reports/2026-10-21-yandex-cloud-acceptance.md).

## Конфиги и секреты

config/controller.json: contact и http_timeout_seconds.
config/registry.json: disabled_modules, autostart_dual_mode, once_timeouts.
config/<algorithm_id>.json читает сам модуль; схемы — [config/README](../../config/README.md).
Старые поля coordination/statistics/lease/grant удалены, загрузчик не игнорирует
их молча. Рабочие файлы в gitignore, шаблоны не содержат токенов.
Токен — окружение ZONT_TOKEN; отдельная get_token утилита скрывает ввод пароля.
