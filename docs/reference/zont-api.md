# Данные и команды ZONT

Реализованный Python API v2 и внутренние ограничения ZONT на 20.10.2026.
Начало работы — [README](../../README.md); точные DTO —
[controller/contracts.py](../../zontcontrol/controller/contracts.py).
Прежние JSON/CLI и Python-клиент прототипа удалены без совместимости.

## Снимок датчиков и контуров

list_devices() возвращает DeviceInfo(device_id, name, model, online).
describe_values(device_id) возвращает ValueDescriptor: key, name, unit,
source, meaning, value_type, current_available, history_available,
history_kind, unavailable_reason. Ключ — ValueKey(device_id, kind, element_id,
field). Он объявляет доступные данные конкретного прибора.

read_current(device_id, value_keys=None) возвращает Snapshot с device_id,
received_at, values и is_atomic=false. Каждый ValueReading содержит key,
value, value_type, unit, source, measured_at, received_at, quality.
quality: good, unknown, missing или stale. Если API не дал время измерения,
measured_at=null; received_at его не подменяет. online=true и status=ok
сами по себе не гарантируют свежесть измерения. null — неизвестное, не ноль.
Несколько последовательных запросов не образуют атомарный снимок.

Нормализация связывает значения по прибору/контуру/датчику и сохраняет:

- показания датчиков value и их качество;
- target_temp выбранного контура — цель, setpoint_temp — расчётная уставка
  теплоносителя из io/z3k-state;
- actual_temp, mode_id (из current_mode), status, worktime и target_sensor_id
  выбранного контура; active, is_off, in_summer_mode и work_mode контура
  сейчас не объявлены в describe_values и не возвращаются как ValueReading;
- смысл состояния переключателя из его подписей.

Служебный индикатор Widget API с ID 0 не является адресуемым датчиком и
пропускается; настоящие датчики питания с положительными ID сохраняются.
Для температурных датчиков обозначение единицы «°» нормализуется в °C,
чтобы единицы фактического значения, истории и целей контура совпадали.

В конфигурации SMART 2.0 владельца контур «Отопление» 20496 регулируется
по воздуху: target_temp — цель воздуха, setpoint_temp — расчётная цель воды.
Скрытый «Котел» тоже доступен, но не подставляется вместо выбранного контура.
Тип consumer сам по себе не доказывает регулирование по воздуху.
current_mode=null означает ручной режим по документации ZONT.
Недоступные значения/пределы сохраняются как null.
Серийные номера, SIM и геопозиция не включаются в публичные DTO.

## История

read_history(device_id, value_keys, start, end) требует явного прибора,
непустого набора объявленных history-ключей и дат с часовым поясом.
Даты нормализуются в UTC. Результат HistoryResult содержит device_id,
start, end, received_at, series, time_truncated, incomplete_reasons.

HistorySeries содержит key, value_type, unit, source, kind, points и
incomplete_reasons. HistoryPoint — measured_at, value, quality.
kind различает measurement и step: показания и ступенчатые состояния.
coverage_start/end выводятся по фактическим точкам. complete требует покрытия
границ, отсутствия причин неполноты, truncation, null и плохого качества.
Это свойство архива, а не дополнительный архитектурный допуск детектора:
детектор сохраняет свои калиброванные проверки и unknown.

Адаптер читает POST /api/load_data: z3k_temperature, z3k_heating_circuit и
web_elements для объявленной истории переключателей. Delta-time Array:
отрицательное время прибавляет своё абсолютное значение к предыдущей метке.
Точки сортируются, ограничиваются интервалом; противоречивые границы частей
и сокращение не маскируются. Пропуски не интерполируются, будущее не используется.
Фасад по умолчанию дробит период на сутки, максимум 31 часть.
Ряды температур и редких переключений имеют разную частоту; worktime —
запрос автоматики, не доказательство горения.

Для исследовательских выгрузок используйте scripts/collect_history.py и
scripts/analyze_history.py через create_read_controller. Они не создают
регистратор/координатор и не запускают алгоритмы.
[Команды и SVG](../../scripts/README.md#исследования-и-калибровка).

## Элементы управления

list_controls(device_id) возвращает ControlDescriptor: key, name, actions,
labels, minimum, maximum, step, hidden, unavailable_reasons.
ControlKey включает device_id, kind, element_id; совпадение element_id
разных приборов не смешивает управление. Скрытые элементы не теряются.
Элементы без документированного действия возвращаются с причиной;
наличие реле/насоса в ответе не создаёт выдуманной команды.

| kind | action | arguments |
| --- | --- | --- |
| circuit | target-temp | target_temp: число |
| mode | activate | duration: минуты, необязательно |
| buttons | trigger | пусто |
| toggle_buttons | trigger | target_state: boolean |
| regulators | set-voltage | target_value: число |
| scenarios | activate | пусто |
| guard_zones | activate | enable: boolean |

Изменение target_temp переводит контур в ручной режим; зависимому контуру
самостоятельное задание температуры запрещено. Активация mode действует
на прибор целиком. Локальная активация режима и schedule не реализованы.
Шаг UI не округляет значение; min/max и типы проверяются по актуальным данным.
Отдельной команды изменения расчётного setpoint_temp не найдено.

Исторически проверенные привязки SMART 2.0 владельца, прибор 700877:
«Отопление» 20496, скрытый «Котел» 20494, режим «Расписание» 20503,
«Сброс котла» 8832. Это примеры, не автоматический выбор приборов.
У сброса true означает «отключен котёл», false — «включен котёл».
Реальный список и подписи берите из list_controls.

## Подготовка и отправка

prepare_command(device_id, control_key, action, arguments) читает актуальные
возможности и возвращает ValidatedCommand(control_key, action, arguments)
без управляющей отправки. arguments в DTO — неизменяемые пары; вызов фасада
принимает mapping. Чистая синтаксическая подготовка без сети доступна
в zontcontrol.commands; она не даёт разрешения управления.

Потребитель получает общий контроллер через app.controller() либо DI:

```python
from zontcontrol.controller.contracts import ControlKey

command = controller.prepare_command(
    42, ControlKey(42, "circuit", 20496), "target-temp", {"target_temp": 22})
# Предпросмотр: команда ещё не отправлена.
```

execute_command(command) повторно проверяет online, актуальные возможности,
элемент, действие и диапазон, затем синхронно отправляет одну команду.
Параллельные команды одному прибору допускаются; приложение не разрешает
конфликты между алгоритмами, другими клиентами и ЛК.

В boiler_cli строка command без --execute показывает preview; с --execute
выполняет отправку. Автономный boiler_reset действует
по собственному контракту без общего preview/execute-флага.
create_read_controller предоставляет только чтение и preview, без execute_command.

CommandResult содержит command, received_at, api_accepted=true,
observed_state=null. Успех ответа API не доказывает физический результат.
Ошибки включают AuthenticationError, DeviceUnavailable, UnsupportedAction,
InvalidArguments, IncompleteHistory, RateLimited, CommandOutcomeUnknown. Произвольные тела ошибок
и credentials не попадают в публичные сообщения.

После отправки timeout/обрыв дают CommandOutcomeUnknown; автоматических
повторов нет. Сначала прочитайте состояние и проверьте прибор.
Отмена или таймаут запуска не отменяет уже отправленный HTTP-запрос.
Алгоритм может отправить off в finally через тот же прямой API.

## Поля устройства

Name/notes не используются приложением для координации или индикации занятости.
При первом адресном чтении прибора ZontAdapter один раз пытается записать name
через POST /api/update_device с единственными полями device_id и name.
Сохраняет исходное имя и заменяет собственный суффикс времени подключения UTC
` [zontcontrol connected <ISO UTC seconds>]`; notes не меняет. Это касается
также create_read_controller. Чтение всех устройств не меняет их имена.
Сбой диагностической записи не блокирует чтение/управление и не повторяется,
включая неизвестный исход. Успешный ответ API не доказывает видимость отметки в ЛК.
Синхронизация удалена полностью по [решению владельца](../history/2026-10-20-remove-synchronization.md).
Исследования возможностей строк остаются [историческим отчётом](../reports/2026-10-14-zont-control-lock-api.md).

## Авторизация

Интерактивная утилита создаёт токен через POST /api/widget/v3/authtokens
с Basic Auth и client_name; пароль вводится скрыто. Каждый запуск создаёт
новый токен. Приложение использует ZONT_TOKEN из окружения, X-ZONT-Client
из controller.json, HTTPS и явный HTTP-таймаут. 401/403 — отказ доступа,
429 — ограничение частоты. Основной транспорт не повторяет запросы;
сборщик архива отдельно повторяет чтение при 429, без управляющих повторов.
Облачный Lockbox пока планируется.

Официальные источники:

- [Widget API v3](https://my.zont.online/api/widget/v3/)
- [OpenAPI v3](https://my.zont.online/api/widget/v3/docs)
- [Полный API](https://lk.zont-online.ru/api/docs/)
