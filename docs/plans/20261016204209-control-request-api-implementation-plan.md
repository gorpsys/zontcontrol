# Issue #4: запрос монополии с состоянием и ограниченным ожиданием

Статус: **реализация начата, Tasks 1–5 завершены**. Приоритет: **P2**.
Задача: [gorpsys/zontcontrol#4](https://github.com/gorpsys/zontcontrol/issues/4).
База: `master`, `6b9174571f2f266b761cdcb1b95cad742fd41027`, версия `0.1.2`.
Подход: TDD, stdlib, Python >=3.11, все проверки офлайн, без деплоя.

## Цель и согласованные границы

Первый ручной --execute должен дождаться разрешения и отправить команду
однократно. Сейчас первая попытка публикует R1 request и возвращает Busy;
CLI не ждёт следующего шага координатора и требует нового ввода.

Потребители — алгоритмы, интерактивный CLI и внешние Python-клиенты.
Управляющий фасад предоставляет асинхронный запрос с идемпотентным чтением
состояния и синхронный запрос с timeout. Три состояния: «запрошено»,
«монополия выдана», «монополия отклонена». Физическое управление не становится
асинхронным: execute_command остаётся синхронным, без повторов после отправки.

Цель, пользователи, данные, интеграции и основные потоки уже определены
issue и договорами. TDD и отсутствие деплоя сохраняются. Уточнение владельца:
async продвигается самостоятельно, даже без опроса статуса, но только в пределах
заказанного срока аренды. Её начало фиксируется при создании публичного запроса;
очередь и повторы не перезапускают этот срок.

Существующий try_acquire_control сохраняет свою реальную немедленную семантику;
новые методы добавляются к API v1. Legacy-адаптеры и дублирующие реализации
протокола не вводятся. Штатные CLI и boiler_reset переходят на новый
синхронный метод; нижняя попытка захвата остаётся общей с немедленным API.
Это совместимая возможность: планируемая версия `0.2.0`, API_VERSION остаётся 1.

Не входят: изменение детектора из issue #5, новые режимы регистратора,
общий сервис выполнения команд, сохранение заявок на диск, распределённая
справедливость, новый протокол R1/R2, автоматический сброс при реализации.

База принятых рисков — [реестр сценариев координации](../reference/coordination-failure-scenarios.md).
R/L/A-сценарии не переоткрываются этим планом и исправляются только при слишком
частом проявлении в эксплуатации. Новые пути, включая C02 (раскрытие
невыданной session), должны быть закрыты до приёмки изменения.

Уточнение границ от 17.10.2026:

- Потеря idle-session при прерывании до возврата handle — принятый A04.
  Ожидание её истечения не является блокером плана; обязательного восстановления
  любой потерянной session или нового recovery API не вводить. Обязательная
  защита нового API — C03: следующая заявка не наследует прежнюю session/ack
  и не получает право за пределами собственного срока.
- Проверки deadline между стадиями ленивой инициализации обеспечивают контракт
  таймаута нового API. Начало чтения или нового coordinator после deadline
  само по себе не доказывает нарушения монополии. Уже начатые стадии могут
  завершиться поздно; ранее запущенный coordinator остаётся независимым ресурсом.
- Проверка reuse/release обеспечивает действительность локальной reservation
  при выдаче GRANTED. Она не усиливает гарантии распределённого протокола.

Приёмка разделяет эти требования: A04/R04 не переоткрываются, а новые выдачи
с неправильными ID/сроком и несогласованный контракт timeout не допускаются.

## Исходный код и договоры

Источники: [архитектура](../agents/architecture.md),
[контракты](../agents/contracts.md), [лидерство](../reference/leader-election.md),
[разработка](../agents/development.md), [сброс](../reference/boiler-reset.md),
[конфиги](../../config/README.md), [выпуск](../../scripts/README.md#версии-и-выпуск).

| Файл | Что переиспользовать или изменить |
| --- | --- |
| controller/contracts.py | Controller, immutable DTO, ошибки. Добавить типы запроса и новые методы без URL/токенов/R1/R2 в контракте. |
| controller/ownership.py | Общая монополия, binding, session identity, begin_close/wait_idle, защита in_flight и pending release. Сохранить их, подключить учёт запросов. |
| controller/service.py | _ConsumerController — передача методов к привязке исполнителя. Чтение/preview не получают монополию. |
| coordination/grants.py | _Pending, uncertain, _ack и повторная публикация потерянного request. Дополнить привязкой к логическому request_id и локальным abandon. |
| application.py | _CoordinationRuntime проксирует gate; composition root передаёт часы/ожидания. Координатор продолжает единолично писать R2. |
| algorithms/boiler_cli.py | _command сейчас делает одну попытку. Заменить получение сессии ограниченным ожиданием. |
| algorithms/boiler_reset.py | _acquire содержит собственный retry-loop; grant_wait_seconds/grant_poll_seconds и расчёт lease уже есть. Перейти на общий sync-метод без смены бюджета сброса. |
| autonomy/contracts.py, autonomy/catalog.py | Объявленные capabilities должны соответствовать новым вызовам потребителей. |
| tests/test_product_flows.py, tests/test_application_coordination.py | VirtualHTTP/FakeZontServer и управляемые часы для реального composition root. |

Новых runtime-зависимостей и удалённых эндпоинтов нет. Runtime остаётся
одним процессом с потоками и обычным DI. Персональные config/*.json и токен
при разработке/проверке не читаются.

## Публичный контракт

Добавить в controller/contracts.py:

- `ControlRequestState`: REQUESTED='requested', GRANTED='granted', DENIED='denied'.
- `ControlRequestReason`: TIMEOUT, CANCELLED, SHUTDOWN, LEASE_EXPIRED,
  CONTROLLER_ERROR; строковые значения задаются явно.
- `ControlRequestErrorCode`: строковый Enum для безопасных ошибок.
  Значения authentication_error, device_unavailable, unsupported_action,
  invalid_arguments, incomplete_history, rate_limited, command_outcome_unknown,
  device_control_busy, control_ownership_required, controller_error соответствуют
  одноимённым существующим типам ControllerError. Типы вне этого фиксированного
  отображения, в том числе неизвестный BaseException, дают controller_error.
  Классификация не меняет retry-политику таблицы переходов ниже.
- `ControlRequest`: frozen dataclass, slots, weakref_slot=True, eq=False;
  поля request_id: UUID, device_id: int; initial_state всегда REQUESTED.
  Это локальный opaque handle, а не сериализуемое доказательство полномочий.
- `ControlRequestResult`: frozen DTO с request_id, device_id, state,
  session: ControlSession|None, reason: ControlRequestReason|None,
  error_code: ControlRequestErrorCode|None. Исключения, traceback/context/cause
  и произвольный текст API в DTO не сохраняются.
  REQUESTED не содержит session/reason/error_code;
  GRANTED содержит session своего device и не содержит причины отказа;
  DENIED не содержит session, содержит reason; error_code обязателен при
  CONTROLLER_ERROR и отсутствует при остальных причинах.
  `make_error() -> ControllerError|None` возвращает None без error_code,
  иначе каждый раз создаёт новый экземпляр соответствующего безопасного типа
  без аргументов. Результат не кэшируется и не сохраняется в DTO/manager.
  Метки времени запроса наружу не нужны.

Методы Controller и соответствующие методы привязки:

```python
request_control(device_id, *, lease_seconds, timeout,
                poll_seconds=1, cancelled=None) -> ControlRequest
get_control_request(request: ControlRequest) -> ControlRequestResult
cancel_control_request(request: ControlRequest) -> ControlRequestResult
acquire_control(device_id, *, lease_seconds, timeout,
                poll_seconds=1, cancelled=None) -> ControlRequestResult
```

`cancelled` — threading.Event или None; это отмена ожидания потребителя,
не deadline регистратора. Невалидные аргументы дают InvalidArguments до I/O:
device_id положительный int; длительности конечные положительные числа,
bool не допускается; `0 < timeout <= lease_seconds`, poll_seconds > 0.
Фактическое ожидание всегда ограничено ближайшей границей timeout/lease,
поэтому короткий timeout допустим и с poll_seconds=1 по умолчанию.
Ограничения lease нижнего gate сохраняются и проверяются до старта worker.
Если вход исполнителя уже закрыт, новый request отклоняется существующей
ControlOwnershipRequired без handle и I/O.

Асинхронное создание только регистрирует локальную заявку, возвращает handle
с initial_state=REQUESTED и будит worker; не ждёт gate/HTTP или grant.
Последующий get может уже вернуть GRANTED: initial_state описывает создание,
не обещает минимальную длительность состояния REQUESTED.

Чтение статуса проверяет тождество выданного handle и его DI-binding:
чужой, сконструированный вручную или не принадлежащий этому executor handle
даёт InvalidArguments без I/O. Знания UUID недостаточно. Получение результата
не вызывает gate, не повторяет request и не продлевает lease. Оно может
локально зафиксировать наступивший timeout/отмену, используя тот же переход,
что worker; это исключает выдачу устаревшего REQUESTED при зависшем HTTP.

Терминальные состояния неизменны. Повторный get возвращает тот же результат
и тот же объект session. GRANTED не превращается в DENIED после release/expiry:
это результат выдачи; текущее право команды проверяется execute_command.
Повторный cancel терминального handle возвращает его терминальный результат.

Каждый вызов request создаёт новый request_id. Пока у binding/device уже есть
REQUESTED, второй request с теми же параметрами возвращает тот же handle;
с другими timeout/lease/poll/cancelled — InvalidArguments. После терминального
исхода новый вызов создаёт новую заявку. UUID генерируется внутри исполнителя.

Sync acquire использует этот же request и механизм состояний; возвращает
только GRANTED либо DENIED. Не возвращает REQUESTED и промежуточный Busy.
Ожидание использует Event/Condition, а не циклический gate I/O в caller.
При timeout/cancelled синхронный caller самостоятельно фиксирует DENIED
под общим lock, не ожидая окончания зависшего HTTP. Уже отправленный HTTP
не убивается; его поздний результат обрабатывает worker.

## Время и переходы состояний

Время — те же локальные BOOTTIME-часы, что у исполнителя и gate.
При создании фиксировать `created_at`, `wait_deadline=created_at+timeout`
и `lease_deadline=created_at+lease_seconds`; при повторах границы не менять.
В gate передавать также `attempt_deadline=min(wait_deadline, lease_deadline)`:
это предел начала следующих стадий попытки, а не срок действия выданной session.
Worker действует только пока now меньше обеих границ. При равенстве
lease_deadline REQUESTED становится DENIED/LEASE_EXPIRED, дальнейших попыток
нет. Ни wall-clock, ни NTP не использовать.

Нижний протокол сохраняет начало remote lease от первой R1-заявки
(`pending.sent`). Ожидание локальной очереди входит и в timeout, и в заказанную
локальную аренду. Gate дополнительно получает local_deadline=lease_deadline
и выдаёт session.expires_at=min(дедлайн подтверждённого R2, local_deadline).
Это делает _Held.session и сессию исполнителя одним и тем же объектом,
с одной более строгой границей; нельзя сокращать срок копированием DTO только
в фасаде, иначе gate отвергнет чужую identity.

R1/R2 не передают абсолютный local_deadline; их форматы, K/rho и доказанные
условия не меняются. Консервативный удалённый grant может сохраняться дольше
локального допуска, но не даёт исполнителю права действовать после
lease_deadline. После первой публикации request_id/nonce/pending.sent неизменны.
Автоматического renew и нового полного lease после ожидания нет.

`timeout <= lease_seconds` не допускает автоматического создания нового R1
request после expiry прежнего в рамках одного публичного handle. На каждой
попытке worker проверяет timeout до gate; при фиксации результата проверяет
его повторно. Даже если gate был вызван вовремя, поздний ack не даёт право
оживить DENIED. Ответственность за lease с запасом на секцию остаётся у алгоритма.

| Событие для REQUESTED | Переход и действие |
| --- | --- |
| Другой локальный владелец или нижний gate Busy | Остаться REQUESTED, назначить следующий poll без нового request_id. |
| Потерянная R1-заявка | Gate повторяет ту же известную заявку по текущим правилам; nonce и sent неизменны. |
| CommandOutcomeUnknown при публикации R1 | Остаться REQUESTED: gate помнит uncertain, дальше только чтение ack; слепого повторения неизвестной записи нет. |
| DeviceUnavailable/RateLimited при получении разрешения | Остаться REQUESTED, следующая попытка не раньше poll_seconds и только до timeout. Не повторять физическую команду. |
| Иная ControllerError или некорректная запись координации | DENIED/CONTROLLER_ERROR с безопасной ошибкой; поток не умирает молча. |
| now >= wait_deadline | DENIED/TIMEOUT. |
| now >= lease_deadline | DENIED/LEASE_EXPIRED; новых попыток нет. |
| cancelled установлен или явный cancel | DENIED/CANCELLED. |
| begin_close | DENIED/SHUTDOWN. |
| Получена session, которая уже истекла | DENIED/LEASE_EXPIRED, cleanup; новой аренды для handle нет. |
| Session действительна, timeout/отмена/shutdown ещё не наступили | GRANTED с точным объектом session из общего исполнителя. |

Если одновременно наступило несколько причин, проверять SHUTDOWN, CANCELLED,
LEASE_EXPIRED, TIMEOUT в этом порядке. После терминального перехода причину не заменять.
Гонку grant/cancel/timeout/lease expiry решает единственная секция под общим lock:
победивший GRANTED остаётся выданным; победивший DENIED сессию не раскрывает.
Если отмена пришла после GRANTED, потребитель сам делает release, как сейчас.

## Реализация запроса и workers

Добавить `controller/requests.py` с ControlRequestManager, внутренним
RequestState и циклом обслуживания. Он получает callbacks общей попытки
захвата, abandon/cleanup, часы и **ту же Condition executor._idle** через DI.
В нём нет HTTP, credentials, форматов R1/R2 или регистратора.

RequestState хранит binding, параметры, created_at/wait_deadline/lease_deadline/next_poll,
результат, completion Event и признак выполняющейся попытки. Переходы,
локальные очереди и `_devices` синхронизируются одним lock Condition;
отдельной независимой копии владения устройством не создавать.

Callbacks: attempt вызывает расширенный executor._acquire с request_id,
local_deadline и attempt_deadline; cleanup вызывает
executor._release_unclaimed(binding, request_id, session=None);
abandon вызывает gate.abandon_request через binding._owner.
Все они выполняются вне Condition; _release_unclaimed проверяет identity и
request_id reservation под общим lock перед пометкой closing. Локальные
проверки admission/параметров при создании request выполняются под Condition
без повторного захвата того же non-reentrant lock.

Вновь созданная request-reservation имеет явное состояние unclaimed:
результат gate ещё не выдан потребителю. `_acquire` сохраняет session в этом
состоянии под общим lock **до** observation_context и любого выхода из lock.
Immediate/reuse и execute не получают unclaimed-session; expiry/drain не
считают её idle даже после возврата `_acquire`, пока manager не принял решение.
Под той же Condition manager либо атомарно помечает session issued и публикует
GRANTED, либо помечает reservation closing для первой попытки cleanup.
Нельзя оставлять окно между публикацией session и установкой этой защиты.
Ранее выданная session при разрешённом reuse остаётся issued, а не unclaimed.

Терминальные результаты хранить в WeakKeyDictionary по выданным handle.
Value не должен держать обратную ссылку на ключ; Result содержит UUID,
а не handle. Активная очередь удерживает handle до терминального состояния
и окончания его cleanup. После этого потребитель сам определяет время жизни
результата своим handle. Нет бессрочного глобального списка всех заявок,
TTL-хранилища, фоновой БД или дополнительных операций forget.

Для CONTROLLER_ERROR хранить только ControlRequestErrorCode. Даже новый
очищенный exception после пользовательского `raise` получает traceback и
может удержать handle через frame. Поэтому ни Result, ни terminal registry
не хранят экземпляры исключений; make_error создаёт независимый экземпляр
при каждом вызове. Пойманные worker-исключения не сохраняются в RequestState.

Для каждого прибора лениво запускать один поток `control-request-<device>`
при появлении непустой очереди без зарегистрированного worker; отдельный
поток на каждую заявку не создавать.
Он обслуживает локальную FIFO-очередь REQUESTED для этого прибора. Внешняя
справедливость R1/R2 не обещается. Другой прибор получает собственный worker,
поэтому его grant не блокируется медленным gate первого.

Регистрацию, запуск Thread и добавление заявки в очередь выполнять под общей
Condition. По окончании последней попытки и cleanup worker под той же Condition
проверяет пустоту очереди и отсутствие своей незавершённой работы. Если очередь
пуста, атомарно удаляет **собственную** запись из manager._workers и пустую
очередь, уведомляет ожидающих и выходит без дальнейших callbacks/HTTP. Если
новая заявка успела попасть в очередь, worker продолжает её обслуживать.
Если заявка пришла после снятия регистрации, caller создаёт нового worker.
Уходящий поток больше не делает gate-попыток и не удаляет запись преемника.
Нет гарантии одного физически живого Thread во время его завершающего выхода,
но для одного прибора одновременно выполняется не больше одной gate-попытки.

Для shutdown отдельно учитывать созданные Thread в WeakSet; это учёт потоков,
не дублирующее владение устройством. Thread заносится туда до start под той же
Condition; при ошибке start регистрация откатывается и заявка получает безопасный
терминальный отказ. Завершённые Thread не хранить сильным архивным списком.
После снятия регистрации у завершающегося worker нет попыток/cleanup и сильных
ссылок manager на его очередь/RequestState. Исчезновение worker не удаляет
выданные session, reservation исполнителя или terminal results живых handles.

Worker проверяет deadlines/cancelled очереди, затем делает не больше одной
gate-попытки для головы очереди. Busy не держит `_devices` или account lock.
Интервал этой заявки берётся из poll_seconds; ожидание прерывается новой
заявкой/отменой/shutdown. Вне callbacks проверять локальные сроки и Event
не реже чем раз в 0.1 с, не превращая пробуждения в дополнительные HTTP-poll.
Во время блокирующего gate/HTTP единственный worker не проверяет очередь:
без get/sync/cancel терминальный статус фиксируется после возврата попытки.
get и ожидающий sync могут локально зафиксировать DENIED раньше, без I/O;
автономность не означает гарантированную задержку смены статуса в 0.1 с.
Срок не продлевается: поздний результат не выдаётся, новые попытки после
deadline запрещены. Завершение уже отправленного HTTP и cleanup может быть
позже deadline. Второго timer-потока и принудительного прерывания HTTP нет.
Пустой worker завершает работу по атомарному правилу выше; при begin_close
завершает cleanup и выходит. В callbacks с gate/HTTP и при join lock не удерживается.

Пока есть REQUESTED либо незавершённая попытка/cleanup после DENIED для binding/device, внешняя немедленная
try_acquire_control не должна подтвердить его grant в обход request manager:
вернуть DeviceControlBusy до gate. Внутренняя попытка worker передаёт request_id
и допускается для этой записи. Проверять это под тем же lock, что создание
заявки, чтобы между проверкой и резервированием не было обхода.

Если binding уже владеет действующей сессией, новый request может получить
тот же session/deadline без продления, только если её expires_at не позже
нового lease_deadline. Иначе создание такого запроса даёт InvalidArguments
до worker/I/O: новый запрос не сокращает и не продлевает ранее выданное право.
Если immediate-acquisition того же binding ещё выполняется при создании
request, handle создаётся как REQUESTED, а worker ждёт локальную reservation
без второго gate-вызова. Когда immediate выдаст session, `_acquire` повторно
проверяет её expires_at под общим lock перед reuse. Если session выходит за
lease_deadline запроса, отказать ему DENIED/CONTROLLER_ERROR с error_code
invalid_arguments; прежнюю issued-session не сокращать, не освобождать и не
вызывать для неё abandon. Если срок подходит, manager повторно проверяет
его и остальные условия при атомарной фиксации GRANTED. Проверки только при
создании handle недостаточно.
В частности, перед GRANTED при reuse под общей Condition повторно проверить,
что `_devices[device_id]` содержит ту же reservation, binding и точный объект
session, reservation остаётся issued и не closing/releasing, а срок session
действителен и помещается в lease_deadline. Если release успел пометить closing
или удалить/заменить reservation, устаревшую session не выдавать и не освобождать
повторно: оставить запрос REQUESTED до следующего poll либо применить уже
наступившую терминальную причину. Если GRANTED зафиксирован первым, последующий
release не меняет результат запроса; execute проверяет текущее право обычным
порядком. Для обоих порядков нужны барьерные тесты.
В `_Ownership` добавить request_id
создавшей reservation заявки (None для немедленного API). Cleanup отвергнутой
заявки освобождает только reservation, созданную ею; не отзывает ранее
выданную тому же binding сессию, которую новая заявка лишь переиспользовала.

## Нижний gate и поздний ответ

В CoordinationGate.try_acquire, _CoordinationRuntime.try_acquire,
ExecutorGate.try_acquire добавить необязательные внутренние request_id,
local_deadline и attempt_deadline. Оба deadline используются только внутри
процесса на общих часах. request_id нового API равен UUID логической заявки;
у немедленного API остаётся
существующее создание ID нижним gate. Получение нового nonce допустимо
только при создании новой заявки. Пока публичный request жив, нижняя
_Pending используется повторно, а не заменяется на каждом вызове.
В _Pending зафиксировать local_deadline и attempt_deadline при создании и не
менять при poll/повторной публикации; _ack проверяет обе границы перед выдачей.
Немедленный API без этих
параметров сохраняет существующее вычисление срока от pending.sent.

### Ленивая инициализация production gate

Это уточнение границ попытки нового API, а не устранение принятого R04 или
добавление гарантии монополии. Не требуется отзывать начатый HTTP либо
привязывать жизненный цикл общего coordinator к таймауту отдельной заявки.

`_CoordinationRuntime.try_acquire` передаёт оба deadline также в
`_device(device_id, *, attempt_deadline=None, local_deadline=None)`, а не только
в уже созданный ExecutorGate. Общая проверка на тех же BOOTTIME-часах выполняется
после получения device lock (ожидание lock тоже расходует срок), перед первым
`adapter.read_metadata`, затем отдельно перед `ExecutorStore.read_r1` и
`read_r2`. Каждый из этих вызовов — самостоятельная стадия; начатый HTTP
может завершиться поздно, следующая стадия после границы не начинается.

Перед публикацией новой пары `(gate, worker)` в runtime._devices и запуском
нового coordinator повторить проверку deadlines и `_closed` под runtime._lock.
Это единая локальная стадия; HTTP под account lock не добавлять. При отказе
выбросить ControlOwnershipRequired, не оставлять частичную запись runtime._devices,
не запускать поток и не писать R1/R2. Созданные, но ещё не запущенные объекты
отбрасываются локально без сетевого cleanup. Manager фиксирует TIMEOUT или
LEASE_EXPIRED по общей таблице; abandon не инициирует повторную инициализацию.

Уже существующий coordinator — независимый общий ресурс: deadline текущей
заявки не останавливает его, не отзывает лидерство и не меняет чужие grants.
Если coordinator запущен до границы, его дальнейшая самостоятельная работа
разрешена после timeout потребителя. Без deadline старые внутренние пути
validate/release сохраняются; эти требования не вводят им срок регистратора.

### Подтверждение и фиксация нижней session

В ExecutorGate.try_acquire после успешного `_ack` создать session пока только
как локальную переменную. Потенциально бросающий `_observe('grant_ack', ...)`
выполнить **до** записи `_Held` и удаления `_Pending`. Затем повторно проверить
remote-derived expiry, local_deadline и attempt_deadline; если срок истёк,
не фиксировать `_Held` и не возвращать session. Только после этих проверок
под существующим device lock записать `_Held`, удалить совпадающую `_Pending`
и вернуть точный объект session без последующих внешних callbacks.

BaseException из observer до commit оставляет только прежнюю _Pending:
локальная reservation executor ещё без session и снимается обычным обработчиком,
manager публикует безопасный DENIED/CONTROLLER_ERROR либо наступившую причину,
затем abandon удаляет совпавшую pending. Неиспользованный remote grant истекает
по принятому A02; выдуманный finished/release без session не отправляется.
Тестовый sink прерывает выполнение на grant_ack, не подменяя настоящий gate.
Событие наблюдения само по себе не подтверждает выдачу права потребителю;
неполный эпизод не трактуется как успешно защищённый.

Этот порядок закрывает конкретное окно внешнего observer, но не обещает
восстановления после любого прерывания между возвратом gate и сохранением
session исполнителем: такая потеря остаётся A04 с ожиданием исходного expiry.
Защита от наследования потерянного права новой заявкой обязательна независимо
от этого порядка observer/commit и проверяется отдельно следующим правилом.

Для request-пути ранний возврат уже имеющейся `_Held.session` также проверяет
соответствие request_id и обоих deadlines. Чужой request_id даёт Busy до
истечения/завершения прежнего grant, а не наследование его session или отзыв.
Штатный reuse ранее issued-session остаётся локальным путём executor с
проверками выше. Новая заявка не может обойти свой срок через нижний cache.

### Стадии попытки и поздний ответ

Для request-пути перед каждой новой стадией gate проверять обе границы:
первичная публикация, `_ack`, отдельное чтение потерянной R1-заявки и её
повторная публикация. Проверку повторять после `_ack` и после чтения R1,
не полагаться на now из начала try_acquire. При истечении не начинать
следующую стадию и не создавать новую _Pending; вернуть ControlOwnershipRequired,
manager выберет LEASE_EXPIRED/TIMEOUT по своим часам до обработки ошибки.
Границы стадий конкретны: `_ack` включает уже принятый `_read` (R2 и R1),
а публикация — один `store.publish_request` с его read/modify/write.
Эти начатые до deadline составные стадии и уже отправленный HTTP могут
завершиться после него; не прерывать их посередине. Это продолжение принятой
попытки, а не разрешение начать следующую публикацию или выдать поздний grant.
Cleanup после отказа отдельно допустим; автоматического повтора неизвестного
исхода нет.

Добавить `abandon_request(owner, device_id, request_id)` в тот же внутренний
DI-маршрут. Это **локальная** операция gate: удаляет только совпадающую
_Pending, не пишет R1/R2, не отзывает действующие grants. Для незнакомого или
уже удалённого request — безопасный no-op. Вызывать её после терминального
отказа и окончания начатого gate-вызова до допуска следующей заявки owner.
Прокси _CoordinationRuntime.abandon_request использует только уже имеющийся
gate из _devices под локальной синхронизацией; не вызывает _device и не
инициализирует устройство/worker через metadata HTTP для отменённой заявки.
Старый request, оставшийся на устройстве, истекает по существующему протоколу;
новая заявка имеет другой request_id/nonce и не принимает его ack.

Во время HTTP RequestState помечен in-flight. Timeout/cancel может сразу
установить DENIED для caller, но запись worker остаётся до окончания вызова.
Успешный поздний gate-ответ:

1. `_acquire` сохраняет вновь полученную session как unclaimed до любых
   внешних callbacks; immediate/reuse, expiry и drain не могут её раскрыть/удалить.
2. Под Condition увидеть терминальный отказ; GRANTED не публиковать.
   Атомарно перевести собственную unclaimed-reservation в closing до передачи
   cleanup, чтобы drain/reacquire не обошли первую попытку release.
3. Выполнить обычный release вне lock; при неизвестном исходе оставить
   reservation до локального expiry и завершения работы без повторных записей.
4. Удалить совпадающую локальную pending-заявку gate и завершить worker-запись.

Отсутствие возвращённой session само по себе не означает, что reservation
пуста: observation_context может бросить BaseException после её сохранения.
При терминальном отказе или аварийном выходе из attempt worker в finally
вызывает `_release_unclaimed(binding, request_id)` даже без возвращённой session;
этот callback под общей Condition находит reservation по binding/request_id.
Только собственную unclaimed переводит в closing, запоминает точный объект
session и запускает штатную первую попытку release вне lock после in-flight.
При переданной session дополнительно проверяет identity. Чужую или ранее
issued-session не меняет; при отсутствующей reservation это no-op. Уже
начатый/выполненный release не дублируется, в том числе после неизвестного исхода.
Для request-пути `_acquire` оставляет сохранённую unclaimed manager для такого
cleanup и не выполняет отдельный конкурирующий release при выходе из
observation_context. После cleanup вызывается abandon совпавшей pending.
Обычный Busy с сохранением REQUESTED не вызывает abandon: request_id/nonce/sent
должны пережить следующую попытку.

Если gate завершился Busy/ошибкой и собственной сохранённой session действительно
нет — только abandon, без команд или выдуманного completion.
Любой BaseException в worker обязан опубликовать
безопасный терминальный исход и снять счётчик активной попытки в finally;
не скрывать активную сессию и не завершать поток с вечным REQUESTED.

## Стыковка с shutdown issue #3

begin_close остаётся без I/O и join: под общим lock закрывает вход, переводит
REQUESTED в DENIED/SHUTDOWN, устанавливает completion и будит workers.
Уже GRANTED не отзываются. При неудаче раннего hook сохраняется принятая
терминальная политика регистратора; запросы не получают обход admission.

wait_idle дожидается `_devices`, выполняющихся попыток manager и late cleanup,
затем под Condition получает сильный snapshot Thread из WeakSet и делает join
вне lock. Снятые с manager._workers, но ещё завершающиеся Thread входят в этот
snapshot. После begin_close новый worker не регистрируется, поэтому этот
порядок не теряет уходящий поток и не требует хранить историю завершённых.
Только после этого существующий
close_resources закрывает координацию/транспорт. Результаты и пустые handle
не задерживают shutdown, status/cancel терминальных handle работают без HTTP
и после close. Idle выданные сессии по-прежнему ограничены local expiry.

Timeout ожидания не продлевает lease и не отменяет принятую физическую команду.
Принудительная смерть host, ненормативная задержка HTTP и документированные
риски R1/R2 не получают новых гарантий от этого изменения.

## CLI, сброс и конфигурация

В BoilerCLIConfig добавить необязательный grant_wait_seconds. Если поле
отсутствует: `min(30, lease_seconds / 2)`. Проверка: конечное положительное
число, не bool, не больше lease_seconds. Старый рабочий JSON остаётся валиден;
личные файлы при реализации не переписываются. Новое поле добавить в
config/boiler_cli.example.json и config/README.md.

_command после preview и --execute вызывает acquire_control с timeout из
grant_wait_seconds, poll_seconds=min(1, timeout) и context.cancelled.
GRANTED: проверить отмену перед execute, отправить ровно одну команду и
release в finally. DENIED: вывести понятную причину и ничего не отправлять.
При CONTROLLER_ERROR CLI получает безопасное сообщение через новый экземпляр
result.make_error(); boiler_reset выбрасывает новый exception из make_error,
чтобы сохранить существующий путь ControllerError. Не хранить исключение
обратно в результате; TIMEOUT/CANCELLED/SHUTDOWN обрабатывать по reason.
Во время ожидания отдельный минутный reader продолжает работать. Отмена
ожидания прерывает stop; quit во время синхронного ожидания будет прочитан
после него, дополнительного потока ввода/IPC не вводить.

BoilerReset._acquire заменяется одним вызовом acquire_control с существующими
grant_wait_seconds, grant_poll_seconds и context.cancelled. При CANCELLED
или SHUTDOWN не начинать импульс; при TIMEOUT сохранить прежний безопасный
путь недоступной монополии без on. Другие ошибки не трактовать как normal.
Расчёт lease_seconds, MINIMUM_SECONDS, 40-секундный импульс, finally и
пятиминутная пауза остаются прежними. Политика повторов сброса остаётся модулю.

CAPABILITIES дополнить новыми методами. BUILTIN_SPECS для обоих модулей
требует acquire_control вместо try_acquire_control; регистратор не получает
новый timeout и не управляет внутренним ожиданием continuous.

## Разработка и проверки

Каждая кодовая задача: падающий тест → минимальное исправление → рефакторинг
→ зелёные целевые тесты и make check. Все edits через apply_patch. Не отмечать
выполненным тест/проверку без фактического результата. После нового изменения
повторять только затронутые проверки и обязательный финальный набор.

Часы, ожидания и UUID передавать тестовым DI; Event/Barrier задают порядок
гонок, sleep не доказывает атомарность. Тесты обязаны разблокировать потоки
и закончить cleanup даже при assertion failure. Использовать настоящие
ExecutorGate и VirtualHTTP для проверки первого CLI --execute; мок, который
сразу возвращает session, такой регрессией не является.

## Implementation Steps

### Task 1: Типы handle и результата запроса

**Files:**

- Modify: `zontcontrol/controller/contracts.py`.
- Modify: `tests/test_controller_contracts.py`.

- [x] Написать RED-тесты состояний/причин, валидных и противоречивых сочетаний
  session/reason/error_code, неизменяемости DTO и object identity handle.
- [x] Реализовать перечисления, ControlRequest и ControlRequestResult;
  не добавлять методы в runtime-checkable Protocol до появления реализации.
- [x] RED-тесты error_code/make_error: фиксированное отображение каждого кода,
  None без ошибки, новый безопасный exception на каждом вызове без аргументов
  и кэширования; неизвестный тип классифицируется как controller_error.
- [x] Проверить слабые ссылки на handle и отсутствие mutable/API-данных в DTO.
- [x] Запустить `python3 -m unittest tests.test_controller_contracts` и
  `make check PYTHON=python3`.

### Task 2: Стабильный ID и локальный abandon нижнего gate

**Files:**

- Modify: `zontcontrol/controller/ownership.py` — внутренний CoordinationGate.
- Modify: `zontcontrol/coordination/grants.py`.
- Modify: `zontcontrol/application.py` — _CoordinationRuntime.
- Modify: `tests/test_executor_grants.py`, `tests/test_application_coordination.py`.
- Modify: `tests/test_control_ownership.py` — только test gate при необходимости.

- [x] Написать RED-тесты одного request_id/nonce/sent на всех попытках,
  потерянной R1-заявки и uncertain без слепой повторной записи.
- [x] Написать RED-тесты local_deadline: очередь/поздний ack не сдвигают
  expires_at, точное равенство не даёт session, gate и executor используют
  один объект session. Добавить request_id/local_deadline/attempt_deadline в _Pending/_ack.
- [x] RED: пересечь attempt_deadline/local_deadline внутри `_ack`, отдельно
  во время следующего чтения R1; не начинать новую публикацию после границы.
  Начатый до неё store.publish_request разрешить завершить с поздним ответом,
  но session не выдавать. Использовать барьеры и тестовые BOOTTIME-часы.
- [x] Добавить необязательные request_id/local_deadline/attempt_deadline по внутреннему DI-маршруту;
  сохранить немедленные вызовы без нового аргумента.
- [x] RED с настоящим ExecutorGate и DI sink, бросающим SystemExit только на
  grant_ack: session не возвращена, `_Held` не сохранён, pending доступна
  abandon. Вызвать sink также с переводом часов за deadline без исключения:
  после observer нельзя commit/вернуть позднюю session. Затем перенести observer
  перед commit _Held/_Pending и добавить повторную проверку сроков.
- [x] RED нижнего cache: request_id старого _Held не подтверждает новую заявку;
  ранний return не обходит local_deadline/attempt_deadline, старый grant не отзывается.
  Отдельно имитировать A04 — session уже в _Held, но не выдана executor:
  новая заявка с меньшим lease получает Busy до expiry, без старой session/ack.
  Принудительное восстановление/удаление старого _Held до expiry не требуется.
- [x] RED ленивой инициализации: барьеры на metadata, read_r1, read_r2 и
  ожидании device lock; перевести часы за timeout/lease, вернуть управление.
  Следующая стадия не начинается, новая пара gate/worker не регистрируется,
  coordinator не стартует. Передать deadlines в _device и добавить проверки
  перед стадиями и общей регистрацией/стартом. Отдельно проверить `_closed`.
- [x] Написать RED-тесты abandon: только совпадающий pending, без I/O,
  без изменения чужого pending/grant, новая заявка не принимает старый ack.
- [x] Смешанный путь: pending от immediate, затем async того же binding;
  новая логическая заявка получает новый request_id/nonce и отвергает старый ack.
- [x] Реализовать abandon_request и согласовать с локальными сроками/очисткой.
- [x] Запустить `python3 -m unittest tests.test_executor_grants
  tests.test_application_coordination tests.test_control_ownership` одной командой
  и `make check PYTHON=python3`.

### Task 3: Менеджер запросов, автономное продвижение и отмена

**Files:**

- Create: `zontcontrol/controller/requests.py`.
- Create: `tests/test_control_requests.py`.
- Modify: `zontcontrol/controller/ownership.py`.
- Modify: `tests/test_control_ownership.py`.

- [x] Сначала RED-тесты локального создания без I/O, неизменного handle,
  самостоятельного продвижения без get, статуса без I/O и GC terminal handles.
- [x] RED GC после CONTROLLER_ERROR: функция с локальными handle/result
  делает `raise result.make_error()`, ошибка ловится снаружи; после удаления
  пользовательских ссылок/exception и gc.collect weakref handle пуста,
  terminal registry освобождён. Проверка только до raise недостаточна.
- [x] Реализовать RequestState/weak registry/очереди и ленивый worker прибора
  на общей Condition, callbacks в текущую реализацию acquisition/release.
- [x] RED накопления: последовательные заявки к разным неизвестным положительным
  device_id завершаются отказом; после cleanup/join живых request-workers снова
  столько же, сколько до теста, в manager нет пустых очередей/регистраций и
  сильного архива завершённых Thread. Затем заявка к действительному прибору
  обслуживается. Gate — офлайн fake или настоящий composition root с VirtualHTTP.
- [x] Барьерный RED ухода worker: последняя заявка очищена, одновременно приходит
  новая для того же прибора. Проверить оба порядка общей Condition: прежний
  worker видит заявку до deregistration либо новый worker создаётся после неё.
  Заявка не теряется, gate-попытки не пересекаются, старый finally не удаляет
  регистрацию преемника. Реализовать атомарное снятие собственной записи и
  освобождение пустой очереди, WeakSet для учёта Thread при shutdown.
- [x] RED begin_close на границе deregistration: новые заявки отклоняются,
  wait_idle делает join уходящего Thread до закрытия ресурсов. При ошибке
  Thread.start нет вечного REQUESTED или незапущенного worker в shutdown snapshot.
- [x] RED-тесты, затем реализация: повтор Busy, таймаут/отмена, точная граница
  deadline, ошибки/uncertain, одновременные cancel/grant, одинаковый терминальный
  результат повторных get/cancel и независимый второй прибор при медленном первом.
- [x] Проверить отсутствие попыток и выдачи после исходного lease_deadline,
  в том числе без единого status polling, после очереди и при задержанном HTTP;
  повторная публикация не начинает новую полную аренду.
- [x] При блокированном HTTP и без get проверить автономный DENIED после
  возврата попытки; отдельно get фиксирует отказ до возврата HTTP.
  Проверка синхронной обёртки выполняется в Task 4.
  Не требовать от занятого единственного worker проверки очереди каждые 0.1 с.
- [x] RED-тесты и реализация request_id в _Ownership: защита от подтверждения
  чужой pending через немедленный API, cleanup только собственной новой session,
  повторное получение уже выданной session без продления или отзыва при cancel,
  отказ создания запроса с lease_deadline раньше существующей session.expires_at.
- [x] Барьерный RED: immediate того же binding ещё in-flight; создать request
  с более ранним lease_deadline, затем выдать immediate-session. Новый request
  получает DENIED/CONTROLLER_ERROR/invalid_arguments, старая session остаётся
  той же issued без release/сокращения/abandon. Повторить с подходящим сроком:
  получить прежнюю session без renew; grant/cancel решаются под общим lock.
- [x] RED reuse/release: остановить manager после возврата ранее issued-session
  из _acquire и до GRANTED; release помечает closing, отдельно удаляет reservation.
  Устаревшая session не выдаётся, лишнего release/renew нет, запрос остаётся
  REQUESTED либо получает наступивший отказ. Обратный порядок: GRANTED раньше
  release, terminal result неизменен, execute после release отклоняется.
- [x] RED-тесты late HTTP success/error/unknown cleanup, первая попытка release
  до expiry-discard, interruption worker и отсутствие вечного REQUESTED.
- [x] RED C02: барьер в observation_context после сохранения session; cancel
  и timeout дают DENIED, immediate того же binding получает Busy, session
  не раскрывается, первая попытка cleanup ровно одна. Второй барьер между
  возвратом `_acquire` и решением manager: expiry/drain не удаляют unclaimed.
  Реализовать unclaimed→issued/closing под общей Condition и запрет reuse.
- [x] RED BaseException в observation_context после сохранения unclaimed:
  callback не возвращает session, worker находит её по binding/request_id,
  публикует DENIED, выполняет одну первую попытку release и завершает drain.
  При неизвестном release reservation остаётся до expiry без повторной записи;
  чужая/issued reservation не освобождается. Общий cleanup работает и без handle session.
- [x] RED-тесты shutdown pending и in-flight, затем включить manager в
  begin_close/wait_idle и join workers. Терминальные handles не удерживают close.
- [x] Проверить успех и все ошибки targeted набором запросов/ownership,
  затем `make check PYTHON=python3`.

### Task 4: Потребительский фасад и синхронная обёртка ожидания

**Files:**

- Modify: `zontcontrol/controller/contracts.py`, `zontcontrol/controller/service.py`.
- Modify: `zontcontrol/controller/ownership.py`, `zontcontrol/controller/requests.py`.
- Modify: `zontcontrol/autonomy/contracts.py` — CAPABILITIES.
- Modify: `tests/test_control_requests.py`, `tests/test_controller_contracts.py`.
- Modify: `tests/test_application.py`.

- [x] RED-тесты четырёх методов Controller, binding identity и forged/foreign
  handle; добавить Protocol и фасадные методы после реализации маршрута.
- [x] RED-тесты acquire_control, затем общая sync-обёртка: terminal result,
  timeout/cancel во время блокированного HTTP без ожидания сетевого завершения.
- [x] Проверить, что неверные параметры и закрытый вход не создают ни worker,
  ни сетевой запрос; статус terminal handle доступен после close.
- [x] Проверить capability declarations и прежние тесты immediate API.
- [x] Запустить целевые тесты контрактов/requests/application и `make check`.

### Task 5: Перевод CLI и boiler_reset на ограниченное ожидание

**Files:**

- Modify: `zontcontrol/algorithms/boiler_cli.py`, `zontcontrol/algorithms/boiler_reset.py`.
- Modify: `zontcontrol/autonomy/catalog.py`.
- Modify: `config/boiler_cli.example.json`, `config/README.md`.
- Modify: `tests/test_boiler_cli.py`, `tests/test_boiler_reset.py`.
- Modify: `tests/test_algorithm_registry.py`, `tests/test_configuration.py`
  и тестовые consumers/mocks, реально затронутые сменой вызова.

- [x] RED CLI: REQUESTED внутри исполнителя не ошибка пользователю, первый
  --execute дожидается GRANTED; DENIED не отправляет команду, stop отменяет ожидание.
- [x] Добавить необязательный grant_wait_seconds, его default/валидацию и
  синхронное получение сессии; успех API/ошибка/unknown command не повторяются.
- [x] RED boiler_reset: отмена/timeout без on, подтверждённая аномалия+grant
  даёт прежний on/finally off; заменить ручной цикл _acquire новым методом.
- [x] RED CONTROLLER_ERROR у потребителей: CLI печатает безопасное сообщение
  из нового exception, reset выбрасывает новый ControllerError нужного типа;
  Result хранит только error_code, ошибка не превращается в normal/повтор команды.
- [x] Обновить required_capabilities и подставные Controller без изменений
  пауз, бюджета once или калибровки. История и unknown/suppressed не меняются.
- [x] Проверить старый CLI JSON без нового поля, неверные значения нового поля,
  все existing CLI/reset tests; выполнить `make check PYTHON=python3`.

### Task 6: Интеграционные сценарии на настоящем composition root

**Files:**

- Modify: `tests/test_product_flows.py`, `tests/test_application_coordination.py`.
- Modify: `tests/test_module_boundaries.py` при новом модуле requests.

- [ ] Первый --execute на свободном виртуальном приборе сам проходит request →
  grant → один send → release, без повторного пользовательского ввода.
- [ ] Async request выдаётся без status polling; get/cancel не создают
  HTTP-запросов. Pending, granted, denied и повторные terminal reads наблюдаемы.
- [ ] Занятый прибор: успех после освобождения и отказ после timeout;
  другой прибор получает grant независимо, пока первый HTTP остановлен барьером.
- [ ] Отмена/timeout во время отправленного request: немедленный DENIED,
  затем поздний grant не исполняется, cleanup не трогает чужое разрешение;
  следующий request имеет новый ID и не принимает старый grant.
- [ ] Через composition root с настоящим ExecutorGate прервать grant_ack
  посредством DI sink: первая заявка с lease=60 получает DENIED, следующая
  с lease=5 не наследует старую session/ack и не превышает собственный срок.
  Пока старый remote grant занят, допустим REQUESTED/отказ; после его окончания
  новая выдача возможна только для новых request_id/nonce.
- [ ] Через composition root остановить первый metadata HTTP, перевести часы
  за deadline и вернуть ответ: новых read_r1/read_r2, request-публикации и
  нового coordinator нет, исходная попытка завершается отказом. Второй сценарий:
  coordinator уже запущен своевременно, timeout заявки его не останавливает
  и не отзывает ранее выданный grant другого binding.
- [ ] Shutdown во время request/late cleanup сохраняет транспорт до конца,
  отменяет pending и не отзывает прежний GRANTED; локальный expiry остаётся рабочим.
- [ ] Запустить `python3 -m unittest tests.test_product_flows
  tests.test_application_coordination tests.test_control_requests` одной командой
  и `make check PYTHON=python3`. Отсутствие физических send при DENIED проверять явно.

### Task 7: Договоры, примеры и выпуск

**Files:**

- Modify: `README.md`, `docs/agents/architecture.md`, `docs/agents/contracts.md`.
- Modify: `docs/reference/leader-election.md`, `docs/reference/boiler-reset.md`.
- Modify: `AGENTS.md` — согласовать правило получения монополии, не расширять хронику.
- Modify: `zontcontrol/__init__.py`, `CHANGELOG.md`, этот план.

- [ ] Описать точные четыре метода, три состояния и terminal semantics,
  примеры async/status/cancel и sync. Immediate API остаётся непосредственной
  попыткой; основной CLI использует sync. Не выдавать API-принятие за физический успех.
- [ ] Описать timeout отдельно от lease/poll, локальное время и поздний cleanup,
  положение о слабом хранении handles и самостоятельном продвижении workers.
- [ ] Сверить новые окна гонок с реестром R/L/A/C: принятые причины не
  переоткрывать, новые пути нарушения не относить к ним по одному результату.
- [ ] Подготовить MINOR `0.2.0`, если база ещё `0.1.2`, иначе выбрать следующий
  свободный MINOR; обновить __version__/CHANGELOG одним выпуском PR.
- [ ] Выполнить `make check PYTHON=python3` и офлайн
  `PIP_NO_INDEX=1 make package-check PYTHON=python3`, включая установку wheel.
- [ ] После коммита выполнить scripts.release_version check с полной SHA
  актуальной базы и полученными тегами. Новые зависимости самовольно не ставить.
- [ ] Проверить критерии ниже и отсутствие секретов/личных конфигов в diff;
  отметить факты выполнения, перенести план в completed с исправлением ссылок.

## Критерии готовности

- [ ] Оба новых режима доступны через фиксированный Controller API; async
  не ждёт сеть, sync возвращает только выдачу или отказ.
- [ ] Опрос идемпотентен: тот же handle/result/session, без gate I/O,
  новых request/nonce или неявного renew; terminal refusal не оживает.
- [ ] Timeout/cancel/close не оставляют доступную caller скрытую session;
  уже отправленная работа доходит до безопасного cleanup без слепых повторов.
- [ ] Terminal-result хранит код ошибки, а не exception; GC-тест после raise
  подтверждает освобождение handle и weak registry.
- [ ] Завершённые запросы не оставляют пустых request-workers/очередей; уход,
  повторный запуск и shutdown синхронизированы одной Condition, у одного
  прибора не пересекаются gate-попытки, сильного архива Thread нет.
- [ ] Unclaimed-session не раскрывается через immediate/reuse и не удаляется
  expiry/drain до атомарного решения manager; C02 закрыт барьерными тестами.
- [ ] Созданный запрос не действует после created_at+lease_seconds; очередь,
  retry и ack не переносят эту границу, local deadline одинаков у gate и session.
  Уже принятые составные metadata-стадии могут завершиться позднее, новые
  стадии после attempt_deadline не начинаются; поздняя session не выдаётся.
- [ ] Mixed immediate/async проверяет deadline в точке reuse; BaseException
  после сохранения unclaimed не оставляет вечную reservation.
- [ ] BaseException на lower grant_ack не оставляет скрытую _Held.session;
  более короткий следующий запрос не наследует отказанное право. Ленивое
  создание устройства проверяет срок между стадиями, не останавливая уже
  работающего coordinator. Reuse/release проверены в обоих порядках.
- [ ] Локальная монополия и expiry issue #3 сохранены; не закрывается транспорт
  под request/командой/release и не освобождается активная принятая работа.
- [ ] CLI выполняет первое явное управление без повторного ввода, reset
  сохраняет собственные интервалы/бюджеты и прежнюю логику аварии.
- [ ] Тесты, wheel, версия и текущая документация согласованы; нет кода issue #5
  и внешних эффектов в офлайн-проверках.

## Отслеживание и внешние действия

Каждый Task выполняется целиком до следующего, после зелёного полного набора.
Отметки [x] только по факту. Новые задачи — ➕, блокеры — ⚠️ с причиной;
при изменении архитектурного решения сначала обновить этот план и договоры.

После реализации отдельный PR с `Fixes #4`. CI — Lint, Offline checks,
Python 3.11/3.12 и wheel smoke. Слияние, автоматический тег и любой деплой
выполняются отдельно; этот документ не даёт команды на них. Ручная физическая
проверка котла для приёмки request API не требуется. Чтение токена, реальные
R1/R2-записи и запуск автоматического сброса в план не входят.
