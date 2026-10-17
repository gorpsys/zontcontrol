# Изоляция межклиентской синхронизации управления устройством

Статус: **план, реализация не начата**. 17.10.2026.
База: обновлённый `master`, `e83871a5c20db068539d6305bcd480cd194f8ade`,
выпуск `0.2.0`, API_VERSION=1.

## Цель и ограничения

Собрать синхронизацию монополии между несколькими клиентами в пакет
`zontcontrol.coordination` с явной границей. Приложение собирает зависимости
и закрывает ресурсы; пакет самостоятельно обслуживает существующий протокол
лидерства и разрешений приборов.

Потребители остаются прежними: CommandExecutor получает разрешения через gate,
алгоритмы и внешние пользователи работают через Controller. Данные пакета —
R1/R2, идентификаторы владельцев/заявок/выдач, аренды, часы и наблюдения.
Схемы данных, последовательности операций и результаты не меняются.

Явные требования владельца:

- **Алгоритм не менять.** Сохранить Fischer/lease, роли R1/R2, wire v1,
  порядок чтений/записей/подтверждений, часы/rho/Delta, deadlines, неизвестные
  исходы, политику повторов, запуск/остановку потоков и статистику.
- **Тесты без отдельного согласования не менять.** Не редактировать и не
  добавлять тесты, фикстуры, mocks, assertions, skips, тестовые настройки,
  discovery или команды проверок. Существующий набор должен пройти целиком.
  Не обходить это ограничение изменением вспомогательного тестового кода.
- Runtime остаётся stdlib, одним процессом с потоками. Отдельный дистрибутив,
  сервис, процесс, IPC, новые зависимости и новая конфигурация не требуются.
- Риски из [реестра](../reference/coordination-failure-scenarios.md) приняты.
  Их повторно не открывать и не исправлять попутно. Новые пути нарушения
  монополии не добавлять; потерянную сессию A04 не выдавать новой заявке C03.
- Работа офлайн, без чтения токенов, личных конфигов, запуска приложения
  против ZONT, реальных команд, выборов лидера и деплоя.

Это рефакторинг с неизменным поведением. Вместо обязательного добавления тестов
из обычного шаблона plan-writer применяются **существующие неизменённые тесты**
как регрессионный oracle. Пользовательское ограничение имеет приоритет.
Если для продолжения потребуется изменить тест, остановить зависимую часть
и представить конкретный тест, причину и предлагаемую правку для согласования.
Не ослаблять production-код, чтобы обойти проверку.

## Текущее устройство

Источники: [архитектура](../agents/architecture.md),
[контракты](../agents/contracts.md), [лидерство](../reference/leader-election.md),
[разработка](../agents/development.md), [выпуск](../../scripts/README.md#версии-и-выпуск).

| Сейчас | Ответственность и проблема границы |
| --- | --- |
| `coordination/leader.py`, `grants.py`, `store.py`, `clock.py`, `models.py` | Уже содержат протокол, хранилища и часы; алгоритм сохраняется. Некоторые импортируют ошибки/session из `controller.contracts`. |
| `application.py::_DeviceCoordinator` | Цикл выбора/продления лидера, обслуживания grant и завершения роли расположен в composition root. |
| `application.py::_CoordinationRuntime` | Многоприборная ленивая сборка store/leader/gate/worker, locks, deadlines и shutdown также находятся в composition root. |
| `controller/ownership.py::CoordinationGate` | Минимальный структурный контракт потребителя gate уже существует. Его сигнатуры и optional observation hook сохраняются. |
| `controller/ownership.py::CommandExecutor`, `controller/requests.py` | Локальная монополия, допуск команд, FIFO/async/sync-заявки и cleanup; остаются в управляющем модуле. |
| `config.py` | Читает конфиг проекта, CoordinationConfig возвращает LeaseTiming. Загрузка конфигов остаётся здесь. |
| `coordination/statistics.py` | Наблюдения используются протоколом и исполнителем. Сохраняются модуль, события, logger и текущие точки вызова. |

Объём — распределённая часть синхронизации и её внутренние зависимости.
Локальные request workers и coordinator устройства — разные потоки;
их нельзя объединять, менять их scheduling или переносить local ownership
в распределённый протокол.

## Целевая граница

```text
application (composition root)
    ├── ZontAdapter, транспорт, конфиги, sink
    ├── CoordinationRuntime(metadata_port, settings, clock, statistics, worker_factory)
    └── CommandExecutor(gate=runtime) → ControllerService → алгоритмы

coordination
    ├── публичные контракты и статические экспорты
    ├── runtime + CoordinatorWorker
    ├── неизменные leader/grants/store/models/clock
    └── неизменные наблюдения

control_types (общие ошибки/session/валидация, только stdlib)
    ↑ coordination и controller.contracts
```

### Общие типы без обратной зависимости на контроллер

Создать `zontcontrol/control_types.py`. Перенести туда **существующие**
ControllerError, InvalidArguments, CommandOutcomeUnknown, DeviceControlBusy,
ControlOwnershipRequired, RateLimited и ControlSession с исходными телами, decorators,
сообщениями, проверками, equality/hash и полями.

Вместе с ними перенести используемые ControlSession функции `_require`, `_id`
и `_number` с исходной семантикой finite/non-bool/OverflowError. Остальные DTO
в `controller/contracts.py` импортируют эти же helpers; копии проверок
и новые варианты валидации не вводить. Другие ControllerError-подклассы
остаются на месте и наследуются от того же импортированного базового класса.

`controller/contracts.py` импортирует перенесённые имена, сохраняя прежние
пути импорта. Во всём runtime должен существовать один объект каждого типа:
`controller.contracts.ControlSession is control_types.ControlSession`.
Это реэкспорт общих типов текущего API, а не преобразователь session и не
legacy-адаптер прототипа. Сессии не копируются и не оборачиваются.

`coordination/leader.py`, `grants.py`, `store.py` и `statistics.py` меняют только импорты
общих типов. Пакет больше не импортирует `controller`, `application`,
`autonomy`, алгоритмы или загрузчик проектных конфигов.
В `statistics.error_event` сохранить локальный импорт CommandOutcomeUnknown
и RateLimited на прежнем месте, заменив только его путь с
`..controller.contracts` на `..control_types`. Классификация событий и все
остальные исполняемые узлы функции остаются прежними.

### Контракты зависимостей пакета

Создать `coordination/ports.py` со структурными typing.Protocol без runtime
`isinstance`-проверок:

- `CoordinationGate` — точный перенос существующего Protocol из ownership:
  `try_acquire(owner, device_id, *, lease_seconds, request_id=None,
  local_deadline=None, attempt_deadline=None)`, `abandon_request`, `validate`,
  `release`. Сигнатуры, типы результатов и ошибки сохраняются.
- `MetadataPort` — существующие `read_metadata(device_id)`,
  `read_metadata_field(device_id, field)` и
  `update_metadata(device_id, field, value)`; никаких HTTP, URL или credentials.
  ZontAdapter структурно реализует этот порт без нового wrapper.
- `CoordinationSettings` — существующие `delta_seconds` и свойство `timing`
  типа LeaseTiming. Передаётся прежний CoordinationConfig; пакет не читает
  JSON и не создаёт вторую копию настроек.
- `Clock` — `now()` и `wait(cancelled, seconds)` в текущем порядке аргументов.
  BootClock, StepClock и FakeClock сохраняют структурную совместимость.

Observation hook сохраняется optional: текущий executor использует
`getattr(gate, 'observation_context', None)`. Не превращать его или `close`
в новые обязательные методы gate для существующих fake-реализаций.
Runtime продолжает иметь observation_context и close для composition root.

### Runtime пакета и жизненный цикл

Создать `coordination/runtime.py`:

- Перенести `_DeviceCoordinator` как `CoordinatorWorker`.
- Перенести `_CoordinationRuntime` как `CoordinationRuntime`.
- Сохранить все атрибуты экземпляров, включая `_devices` с парами
  `(gate, worker)`, `_device_locks`, `_lock`, `_closed`, adapter/config/clock/statistics.
- Сохранить сигнатуры существующих рабочих методов и весь порядок их действий:
  ленивую инициализацию, проверки сроков после lock и между metadata/R1/R2,
  защиту старта, cancel/join, cleanup и отсутствие I/O в abandon.
- Добавить только DI-параметр `worker_factory=None` в конструктор runtime;
  default — CoordinatorWorker. В прежней точке создания worker вызвать
  сохранённую фабрику с теми же аргументами. Других изменений управляющей
  логики методов нет.

Пакет не открывает транспорт/файлы, не загружает конфиги, не получает токен
и не управляет sink. Импорт пакета и конструктор runtime не начинают HTTP
или потоки: ленивая модель сохраняется. Ресурсы закрывает application в
прежнем порядке, после завершения executor/алгоритмов.

### Публичная поверхность и assembly

`coordination/__init__.py` содержит только статические реэкспорты и `__all__`:

```text
CoordinationRuntime, CoordinatorWorker,
CoordinationGate, MetadataPort, CoordinationSettings, Clock,
BootClock, LeaseTiming,
Statistics, JsonlSink, ObservationError, observe, error_event, analyze, read_jsonl
```

Основные production-потребители за пределами пакета импортируют только
эту поверхность. Внутренние leader/grants/store/records остаются внутренними
деталями, не становятся аргументами Controller/алгоритмов. Их текущие пути
сохраняются для неизменённых прямых тестов протокола.

`scripts/analyze_orchestration.py` также является внешним потребителем:
его единственный импорт ObservationError/analyze/read_jsonl перевести с
`coordination.statistics` на `coordination`. Функции и исключение реэкспортируются
как те же объекты; разбор JSONL, вывод, exit codes и поведение скрипта неизменны.
Это офлайн-утилита, а не тестовая инфраструктура; её существующий тест
`tests.test_orchestration_statistics` не меняется.

В `application.py` остаются сборка зависимостей и factory seam:

```python
from .coordination import CoordinationRuntime
from .coordination import CoordinatorWorker as _DeviceCoordinator

def _create_device_coordinator(*args, **kwargs):
    return _DeviceCoordinator(*args, **kwargs)

# В существующей точке create_application, с прежними объектами DI:
coordination = CoordinationRuntime(
    adapter, config.coordination, clock, statistics,
    worker_factory=_create_device_coordinator,
)
```

Именно позднее разрешение имени `_DeviceCoordinator` в assembly-функции
сохраняет `patch('zontcontrol.application._DeviceCoordinator')` в существующем
тесте. Простого import-alias без передачи фабрики недостаточно: runtime
иначе найдёт класс в globals нового модуля. Не захватывать класс значением
default-аргумента/lambda до установки patch. Не использовать sys.modules,
подмену globals нового модуля или дублирование прежнего цикла.

BootClock по-прежнему создаётся в application из импортированного там имени,
сохраняя `patch('zontcontrol.application.BootClock')` и обработку его ошибки.
Application._coordination продолжает ссылаться на реальный runtime,
без прокси, скрывающего используемые текущими тестами `_devices`/locks.
`create_read_controller` не начинает координацию.

CommandExecutor остаётся владельцем локального mutex/request manager;
в ownership меняются только импорты CoordinationGate и наблюдений.
`controller/requests.py`, потребительские алгоритмы и Controller API
не меняются. `config.py` импортирует LeaseTiming через поверхность пакета.

## Что должно остаться неизменным

1. Сигнатуры и результаты Controller API v1, типы ошибок, точные объекты
   session/handle, перечень capabilities и конфиги.
2. Лидерство/20-минутный предел, поправки часов, R1/R2 и имя с исходным
   префиксом, идентификаторы, revisions, чтения и записи, неизвестные исходы.
3. Async/sync/immediate семантика, исходные deadlines, очередь и retirement
   request-workers, unclaimed/issued/closing, первая попытка release и finally.
4. Независимость приборов и общего coordinator от таймаута потребителя.
5. Формат, logger `zontcontrol.coordination.statistics` и точки наблюдений.
6. Импульс сброса/детектор/cooldown/бюджеты алгоритмов, CLI и deploy.
7. Все файлы существующих тестов, фикстур и тестовой инфраструктуры.

## Проверка без изменения тестов

На старте реализации сохранить базовый SHA и результаты существующего набора.
У базы 0.2.0: 549 unittest, из них 4 installed-only пропуска; wheel smoke —
5 проверок, включая эти 4. Числа не подгонять: необъяснимое изменение discovery,
дополнительный skip, зависание или изменение ожидаемого исхода — блокер.

Сравнение тестов после каждой задачи и перед завершением:

```sh
git diff --exit-code e83871a5c20db068539d6305bcd480cd194f8ade -- tests
git ls-files --others --exclude-standard -- tests
```

Первый вывод должен быть пуст и exit=0; второй не должен показывать новых
неигнорируемых файлов. Отдельно проверить отсутствие изменений Makefile,
CI workflows и исполняемых scripts, меняющих discovery/пропуски/проверки.
Обновлять только номер выпуска и документацию по задаче 4.

Дополнительные **разовые диагностические команды**, без создания тестовых
файлов и без изменения test suite:

- Через AST сравнить с базой тела `leader.py`, `grants.py`, `store.py`,
  `clock.py`, `models.py`, `statistics.py`, исключив только верхнеуровневые
  Import/ImportFrom и номера строк. Различий исполняемых узлов быть не должно.
  Единственное дополнительное разрешение — путь локального ImportFrom внутри
  `statistics.error_event`: нормализовать ровно замену `..controller.contracts`
  на `..control_types` при неизменных импортируемых именах и положении узла.
  Другие вложенные импорты/узлы не исключать из сравнения.
- Для перенесённых worker/runtime сравнить методы с прежними классами:
  допустимы переименование классов, type annotations и явно описанные
  изменения constructor/factory call. Другие ветви, lock scopes, waits,
  exception clauses, HTTP-последовательности и finally должны совпасть.
- Разрешить относительные импорты через importlib.util.resolve_name в AST.
  Внутри coordination разрешены только stdlib, собственные модули и
  `zontcontrol.control_types`. В control_types — только stdlib. Внешние
  production-модули не импортируют внутренние submodules coordination.
  Область аудита — все отслеживаемые `.py` в `zontcontrol/` и `scripts/`;
  каждый найденный внутренний импорт вне coordination считается нарушением.
  `tests/` исключены из этого правила фасада: их прежние прямые импорты
  протокольных модулей намеренно сохраняются. Никаких молчаливых исключений
  для анализатора статистики нет.
- Проверить identity реэкспортированных типов и отсутствие импортов
  application/controller/config в графе coordination. Для проверки изоляции
  импортировать пакет в отдельном Python-процессе без создания runtime;
  это не запускает приложение и не требует окружения/сети.
  Проверяемые пары identity: каждый из семи перенесённых типов в
  controller.contracts/control_types; CoordinationGate в ownership/ports;
  BootClock и LeaseTiming в facade/clock; Statistics, JsonlSink,
  ObservationError, observe, error_event, analyze, read_jsonl в facade/statistics;
  application._DeviceCoordinator/facade.CoordinatorWorker. Проверить также
  неизменность наследования остальных ControllerError-подклассов.

Эти диагностики подтверждают перемещение кода и границы. Они не заменяют
существующие поведенческие тесты и не усиливают принятые гарантии протокола.

## Implementation Steps

### Task 1: Общие типы и контракты зависимостей

**Files:**

- Create: `zontcontrol/control_types.py`, `zontcontrol/coordination/ports.py`.
- Modify: `zontcontrol/controller/contracts.py`, `zontcontrol/controller/ownership.py`.
- Modify: `zontcontrol/coordination/leader.py`, `grants.py`, `store.py`, `statistics.py`.
- Tests: существующие файлы, **без изменений**.

- [x] Зафиксировать исходный SHA и неизменённый baseline: `make check PYTHON=python3`.
- [x] Перенести семь общих типов и три helper-функции; оставить identity-реэкспорты
  в controller.contracts без копий session/ошибок и изменений валидации.
- [x] Создать четыре Protocol; CoordinationGate перенести без расширения
  обязательных методов, сохранив доступность импортированного имени в ownership.
- [x] Заменить зависимости leader/grants/store и локальный импорт в
  statistics.error_event на общие типы; тела протокола оставить неизменными.
  Подтвердить AST-сравнение с единственным разрешённым nested-import
  изменением и identity классов, включая RateLimited.
- [x] Выполнить существующие `tests.test_controller_contracts`,
  `tests.test_control_ownership`, `tests.test_control_requests`,
  `tests.test_leader_election`, `tests.test_executor_grants`,
  `tests.test_coordination_store` одной командой unittest. Проверить отсутствие
  изменений/новых файлов tests. Не переходить дальше при падении.

### Task 2: Перенос runtime и coordinator worker в пакет

**Files:**

- Create: `zontcontrol/coordination/runtime.py`.
- Modify: `zontcontrol/application.py`, `zontcontrol/coordination/__init__.py`.
- Tests: существующие файлы, **без изменений**.

- [ ] Перенести оба класса с сохранением методов, атрибутов, timing и порядка
  операций; добавить только worker_factory DI в указанных двух местах.
- [ ] Объявить экспорты CoordinationRuntime/CoordinatorWorker; собрать runtime
  в create_application и сохранить позднюю factory seam для прежнего patch.
- [ ] Удалить тела старых классов из application, не оставлять второй реализации.
  Сохранить там создание BootClock, транспорт/sink и порядок закрытия ресурсов.
- [ ] Сравнить методы с базой по правилам выше; проверить существующие DI seams,
  `_devices` tuple shape, отсутствие запуска при import/read-only controller.
- [ ] Выполнить неизменённые `tests.test_application`,
  `tests.test_application_coordination`, `tests.test_product_flows`,
  `tests.test_control_requests` одной командой unittest. Проверить отсутствие
  изменений/новых файлов tests. Любое требование изменить patch/fixture
  вынести на отдельное согласование, а не исправлять тест автоматически.

### Task 3: Закрыть импорты за поверхностью пакета

**Files:**

- Modify: `zontcontrol/coordination/__init__.py`, `zontcontrol/application.py`.
- Modify: `zontcontrol/config.py`, `zontcontrol/controller/ownership.py`.
- Modify: `scripts/analyze_orchestration.py` — только импорт публичных объектов.
- Tests: существующие файлы, **без изменений**.

- [ ] Оформить перечисленный `__all__` статическими реэкспортами; не добавлять
  service locator, динамическую загрузку, глобальный runtime или side effects.
- [ ] Перевести application/config/ownership и analyze_orchestration на импорты
  через пакет. Внутренние модули используют собственные submodules/ports и
  neutral control_types; тела офлайн-анализатора и протокола неизменны.
- [ ] Выполнить AST-аудит разрешённых зависимостей, identity классов и сравнение
  алгоритмических модулей. Исправлять только расположение/импорты/assembly.
- [ ] Запустить неизменённые `tests.test_module_boundaries`,
  `tests.test_configuration`, `tests.test_orchestration_statistics`,
  `tests.test_application_coordination`, `tests.test_control_ownership` и
  `tests.test_control_requests`. Сохранить logger и optional hook семантику.
- [ ] Проверить неизменность tests/тестовой инфраструктуры и green результат;
  зафиксировать фактическую границу в журнале реализации.

### Task 4: Документация, выпуск и полная приёмка

**Files:**

- Modify: `docs/agents/architecture.md`, `docs/agents/contracts.md`,
  `docs/agents/README.md`, `AGENTS.md`.
- Modify: `zontcontrol/__init__.py`, `CHANGELOG.md`, этот план.
- Modify: `README.md` — только если требуется уточнить внутреннее устройство;
  новых пользовательских команд нет.
- Tests и скрипты проверок: существующие, **без изменений**.

- [ ] Описать ответственность пакета, dependency direction, общий слой типов,
  внутренние детали и assembly seam. README не выдаёт структурный перенос
  за новую функцию или улучшение надёжности протокола.
- [ ] Подготовить PATCH `0.2.1`, если свободен; иначе следующий свободный PATCH
  актуальной базы. Controller API_VERSION=1 и wire v1 не менять.
- [ ] Убедиться, что AST/identity-аудит и git-сравнение тестов чистые,
  алгоритм/параметры/конфиги/риски не менялись, нет личных файлов/секретов в diff.
- [ ] Выполнить `make check PYTHON=python3`, затем
  `PIP_NO_INDEX=1 make package-check PYTHON=python3`. Все прежние тесты
  и installed smoke проходят; число и причины skips остаются прежними.
- [ ] После подписанного коммита выполнить `python3 -m scripts.release_version
  check --base <полная SHA актуальной базы>` с полученными тегами. Не создавать
  и не переносить теги вручную. Факты выполнения отметить без выдуманных checks.
- [ ] Подтвердить критерии ниже, перенести план в completed с исправлением
  ссылок. При последующей правке code/package повторить затронутые проверки.

## Критерии готовности

- [ ] Распределённая синхронизация и её runtime находятся в coordination,
  application содержит только assembly/lifecycle и необходимую DI seam.
- [ ] У пакета нет импортов контроллера/приложения/алгоритмов/конфиг-загрузчика;
  исполнителю доступны только согласованный gate и общие типы/наблюдения.
- [ ] Нет копий алгоритма, преобразования session или второго локального mutex.
- [ ] Протокол, сроки, wire, I/O-порядок, блокировки, finally и риски неизменны.
- [ ] Все прежние тесты и инфраструктура побайтово неизменны; существующие
  549 unittest и 5 wheel smoke проходят с прежними 4 source-only skips.
- [ ] Сохраняются BootClock/worker patch seams, приватные состояния, используемые
  текущими тестами, и корректный shutdown на старых сценариях.
- [ ] Документация/версия/wheel согласованы; нет реальных воздействий ZONT.

## После реализации

Отдельный PR с результатами неизменённых тестов и описанием структурной границы.
Публикация, squash merge, автоматический release tag и деплой — отдельные
действия; этот план не запускает их. Холодное ревью плана или кода можно
провести по отдельной команде владельца.
