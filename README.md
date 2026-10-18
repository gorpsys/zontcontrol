# zontcontrol

Локальное приложение управления отоплением ZONT: Python API v1, разовые и
постоянные алгоритмы, интерактивный CLI, история и SVG-графики. Python 3.11+,
runtime использует только stdlib. При подтверждённом температурном паттерне
boiler_reset выполняет настоящий 40-секундный сброс. Облачный запуск —
[будущая работа](docs/plans/yandex-cloud.md).

Это полная замена прототипа без совместимости прежних Python/CLI/JSON.
Текущий подготовленный выпуск — 0.2.1; завершение реализации не означает
слияния PR, публикации тега или деплоя.

## Установка и конфигурация

Из checkout команды выполняются из корня проекта:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/python -m zontcontrol --help
.venv/bin/python -m zontcontrol --version
cp config/controller.example.json config/controller.json
cp config/registry.example.json config/registry.json
cp config/boiler_reset.example.json config/boiler_reset.json
cp config/boiler_cli.example.json config/boiler_cli.json
```

Проверьте контакт, ID приборов, контуров, датчиков и управления в своих файлах.
Примеры содержат условные привязки. Для headless используйте
config/registry.headless.example.json вместо registry.example.json: он отключает
интерактивный boiler_cli. Настройки координации в примере не обмерены для ZONT.

controller.json задаёт контакт, HTTP-таймаут, координацию и путь статистики;
registry.json — disabled_modules, autostart_dual_mode и once_timeouts.
Каждый модуль сам читает config/\<algorithm_id\>.json и проверяет свою схему.
Общих --config/ZONT_CONFIG нет. Поля и примеры — [config/README](config/README.md).

Для wheel подготовьте отдельный рабочий каталог с таким же config/ и
запускайте установленный CLI из него:

```sh
python3 -m venv /tmp/zontcontrol-venv
/tmp/zontcontrol-venv/bin/python -m pip install /absolute/path/zontcontrol-0.2.1-py3-none-any.whl
cd /absolute/path/zontcontrol-work
/tmp/zontcontrol-venv/bin/zontcontrol list
```

Каталог zontcontrol-work с заполненными config/*.json создаётся заранее.
Wheel содержит пакет без конфигов, docs и scripts. Поиска конфигов вверх
по дереву или относительно site-packages нет. --help и --version
не требуют файлов, токена и сети.

## Токен

Получить новый токен отдельной интерактивной утилитой:

```sh
python3 -m zontcontrol.get_token --contact YOUR_EMAIL
```

Пароль вводится скрыто; каждый запуск создаёт токен и выводит его на экран.
Основное приложение получает токен только из ZONT_TOKEN. Например, для уже
сохранённого локального токена:

```sh
export ZONT_TOKEN="$(cat "$HOME/.tokens/zont")"
```

Токены и пароли не передаются в аргументах и не записываются в JSON, Git или
журналы. Контакт X-ZONT-Client в controller.json не обязан совпадать с логином.
Утилита токена использует свой --contact; сборщик архива — ZONT_CONTACT.

## Запуск алгоритмов

```sh
python3 -m zontcontrol list
python3 -m zontcontrol once boiler_reset --timeout 180
python3 -m zontcontrol all
python3 -m zontcontrol start boiler_reset
python3 -m zontcontrol host --start boiler_reset
```

list показывает регистрацию и состояние; once запускает один разовый модуль,
all — все доступные once. once, all, start и host с boiler_reset могут
выполнять настоящий сброс при текущем confirmed_pattern. Общего
preview/execute-флага у автономных алгоритмов нет. boiler_reset поддерживает
оба режима и по умолчанию не стартует сам; autostart_dual_mode=true включает
его при регистрации. boiler_cli поддерживает continuous и запускается при
регистрации, если разрешён и доступен терминал. Для once без UI отключите
его в registry.json.

start и host удерживают один процесс. host допускает несколько --start ID;
SIGINT/SIGTERM инициируют кооперативное завершение. Отдельной команды stop
в другом процессе нет: Python API stop действует на тот же экземпляр.

```python
from zontcontrol.application import create_application

with create_application() as app:
    print(app.list_algorithms())
    print(app.list_continuous())
    # app.run_once("boiler_reset", timeout=180) выполняет реальное действие
    # app.run_all_once() запускает все разрешённые once
    # app.start("boiler_reset") запускает постоянный модуль
    # app.stop("boiler_reset", wait_timeout=5) запрашивает остановку
    # app.host() удерживает процесс до сигнала
```

close() вызывается контекстным менеджером. Ограниченное ожидание stop/close
может завершиться раньше потока: stopping и монополия сохраняются до выхода
и finally; повторный запуск запрещён. Поток принудительно не убивается.
Timeout once выставляет отмену; continuous сам задаёт свои интервалы
и таймауты, общего deadline регистратора у него нет.

Внешний Python-потребитель получает собственный фасад через app.controller().
Он сам завершает составное действие и освобождает сессию, например:

```python
from zontcontrol.application import create_application
from zontcontrol.controller.contracts import ControlRequestState


def run_external_section(controller, on, off):
    # on/off — заранее подготовленные ValidatedCommand одного прибора.
    result = controller.acquire_control(on.device_id, lease_seconds=120, timeout=30)
    if result.state is ControlRequestState.DENIED:
        error = result.make_error()
        if error is not None:
            raise error
        print(result.reason.value)
        return
    session = result.session
    try:
        controller.execute_command(on, session)
        # Здесь работа потребителя между on и off.
    finally:
        try:
            controller.execute_command(off, session)
        finally:
            controller.release_control(session)


with create_application() as app:
    controller = app.controller()
    # Подготовьте команды через controller.prepare_command(...).
    # run_external_section(controller, on, off) отправляет реальные команды.
    # Сессия освобождается до выхода из with и вызова app.close().
```

lease_seconds выбирает потребитель на получение grant, всю секцию, finally
и запас; 120 секунд в примере не являются общим бюджетом приложения.
timeout ограничивает ожидание выдачи; оба срока считаются от создания запроса
и не продлеваются очередью/опросом. DENIED не даёт session и не отправляет
команду. Успех execute_command означает ответ API, а не физический результат.

Асинхронный вариант возвращает локальный handle без ожидания сети. Worker
продвигает его самостоятельно; get/cancel не выполняют HTTP:

```python
def request_then_cancel(controller, device_id):
    request = controller.request_control(device_id, lease_seconds=120, timeout=30)
    status = controller.get_control_request(request)
    print(status.state.value)  # requested, granted или denied
    result = controller.cancel_control_request(request)
    # Grant мог выиграть гонку с cancel. Terminal результат уже не меняется.
    if result.state is ControlRequestState.GRANTED:
        controller.release_control(result.session)
    return result
```

Handle сохраняют, пока нужен статус; terminal registry хранит его слабо.
Повторные get/cancel дают тот же terminal result даже после release/expiry.
GRANTED фиксирует выдачу, текущее право проверяется при каждой команде.
acquire_control возвращает только GRANTED/DENIED; try_acquire_control остаётся
непосредственной попыткой с DeviceControlBusy без ожидания разрешения.
Все четыре новых метода, error_code/make_error и поздний cleanup описаны
в [контрактах](docs/agents/contracts.md#запрос-монополии-api-v1).

close сначала запрещает новые app.controller() и захваты, затем отменяет
зарегистрированные алгоритмы, ждёт их finally и освобождения всех сессий
исполнителя, включая внешние, и закрывает координацию и транспорт.
Владелец действующей сессии может закончить команды и finally до release.
Сессия между командами задерживает закрытие до release или expiry; пустой
фасад — нет. Тот же lease ограничивает локальное владение: idle-сессия после
expiry удаляется без сетевой команды. Acquisition, принятые команды и уже
запрошенный release завершаются до освобождения их локальной reservation.

Регистратор не отменяет внешние потоки: сигнал завершения и release в finally
организует их владелец. app.close(wait_timeout=0) начинает shutdown и может
вернуть False с открытыми ресурсами; после cleanup повторный app.close()
дожидается той же операции и возвращает True. Нельзя вызвать полный close()
в том же потоке до собственного release: он задержит cleanup до expiry.
Сначала завершите секцию и release либо используйте ограниченный close,
затем cleanup и полный close. Произвольные внешние чтения без сессии в это
ожидание не входят; завершите их до закрытия приложения самостоятельно.

Если ранний hook закрытия входа завершился ошибкой, close выдаёт безопасный
RuntimeError, запрещает новые старты алгоритмов и публикует им отмену.
Это терминальный сбой: повторный close снова выдаст ошибку, hook не повторяется,
координация и транспорт остаются открытыми. Событие результата shutdown в этом
случае не означает закрытия ресурсов. Владелец всё ещё отвечает за cleanup
действующей сессии; shutdown не продлевает R2 и не гарантирует finally после kill.

## Чтение и ручное управление

Чтение без регистратора, фоновых алгоритмов и координатора:

```python
from zontcontrol.application import create_read_controller

controller = create_read_controller()
for device in controller.list_devices():
    print(device)
    print(controller.describe_values(device.device_id))
    print(controller.read_current(device.device_id))
```

Для истории используйте read_history(device_id, value_keys, start, end) с
timezone-aware датами; ключи берите из describe_values. null означает
неизвестное значение, measured_at=null — неизвестное время измерения;
received_at не подменяет его. Контур «Котел» не подставляется вместо
«Отопление». [Контракты и методы](docs/reference/zont-api.md).

Интерактивный boiler_cli обновляет таблицу раз в минуту. help показывает
доступные действия; адресаты ограничены config/boiler_cli.json и возможностями
прибора. Пример строки внутри UI с условными ID:

```text
command 42 circuit 20496 target-temp '{"target_temp":22}'
command 42 circuit 20496 target-temp '{"target_temp":22}' --execute
graph 42 air 2026-10-15T10:00:00+03:00 2026-10-15T12:00:00+03:00
```

Ручная команда показывает предпросмотр; первый --execute ожидает монополию
через acquire_control в пределах grant_wait_seconds и отправляет команду один
раз. По умолчанию ожидание — min(30, lease_seconds/2); отмена/отказ не отправляют
команду. Чтения и предпросмотр не блокируются
занятым прибором. SVG сохраняется на диск; --view явно открывает его браузером.
Графики строятся stdlib с настоящим временем, quality и разрывами истории.

## Сброс и ограничения

Детектор оценивает последние 45 минут независимо от прежних запусков.
Только текущий confirmed_pattern разрешает сброс; warning не запускает его,
unknown сохраняется при недостатке данных, suppressed означает намеренное
отключение/паузу. normal не доказывает исправность котла. Пороговый профиль
SMART 2.0 и калибровка сохранены: [детектор](docs/reference/anomaly-detection.md).

boiler_reset включает переключатель сброса на 40 секунд, в finally сразу
пытается выключить его при остановке или исключении. Изначально включённый
сброс не трогает; ручной on во время импульса не отменяет off. Continuous
после off ждёт пять минут до новой проверки. Успех API не доказывает
восстановления нагрева. [Поведение модуля](docs/reference/boiler-reset.md).

Все воздействия проходят через монополию на прибор и grant R2. Принятая до
истечения аренды команда завершается даже после stop/timeout; новые команды
после истечения, включая finally, отклоняются. Потеря роли лидера не отменяет
сессию алгоритма. Kill/смерть host не гарантируют выключение сброса.

В name/R2 видна занятость с сохранением исходного имени. Прямой ЛК остаётся
доступен: человек контролирует редкие вмешательства и принимает риск.
Локальный mutex не блокирует ЛК и другие исполнители. Схема R1/R2 принята
с известными гонками и сбором статистики; строгая распределённая безопасность,
линеаризуемость ZONT и достигнутые 99.9% не доказаны.
[Лидерство](docs/reference/leader-election.md),
[учёт риска](docs/reference/orchestration-risk.md).

При CommandOutcomeUnknown после отправки команда могла выполниться.
Автоматического повтора нет: прочитайте состояние и проверьте прибор.
При 401/403 проверьте доступ, при 429 дайте API паузу. При unknown проверьте
историю, привязки датчиков и состояние сброса.

## Разработка

- [Команды, офлайн-проверки, wheel и выпуск](scripts/README.md).
- [CHANGELOG](CHANGELOG.md), [история реализации](docs/history/2026-10-16-architecture-implementation.md).
- [Индекс инструкций](docs/agents/README.md), [конфиги](config/README.md).
- [Облачный план](docs/plans/yandex-cloud.md), [контракт деплоя](docs/agents/deployment.md).
