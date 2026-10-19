# Контракты приложения

Описывает реализованное локальное приложение 0.3.0, Controller API v1 и
wire v2 на 19.10.2026.
[Архитектура](architecture.md) раскрывает границы и решения; точные DTO и
ошибки доступны через zontcontrol/controller/contracts.py, общие типы определены
в zontcontrol/control_types.py, алгоритмы — в zontcontrol/autonomy/contracts.py.
Прототип удалён без обратной совместимости.

## Слои и входы

controller/auth выполняет авторизацию, controller/transport — HTTP с явным
таймаутом, controller/zont — вызовы ZONT и нормализацию через inventory/history.
controller/service предоставляет потребительский API и ownership; coordination
отвечает за R1/R2, ленивый runtime, coordinator workers и наблюдения.
commands готовит и проверяет команды;
anomaly/monitor/detection_profile сохраняют чистую логику детектора.
autonomy/runtime и registry управляют потоками и состоянием; application
связывает зависимости. algorithms/boiler_reset и boiler_cli независимы.

zontcontrol и python -m zontcontrol вызывают application.main; утилита токена —
zontcontrol-get-token или python -m zontcontrol.get_token. Старых client.py,
cli.py, control.py, ZontClient, apply_command и entrypoint zontcontrol-control нет.
Legacy-адаптеры и реэкспорты интерфейсов прототипа не вводить. Исследовательские скрипты используют
create_read_controller без регистратора и координатора.

create_application предоставляет controller(), list_algorithms(mode=None),
list_continuous(), run_once(id, timeout=None), run_all_once(timeouts=None),
start(id), stop(id, wait_timeout=0), host(cancelled=None), close(wait_timeout=None)
и контекстный менеджер. wait_timeout передаётся по имени. CLI: list,
once ID [--timeout SECONDS], all, start ID, host [--start ID]. stop работает
только в том же процессе через Python API; host штатно завершается по
SIGINT/SIGTERM. IPC нет.

## Граница пакета coordination

Production-потребители используют статические реэкспорты zontcontrol.coordination:
CoordinationRuntime, CoordinatorWorker, CoordinationGate, MetadataPort,
CoordinationSettings, Clock, EpochSampler, BootClock, LeaseTiming, ProtocolProfile,
Statistics, JsonlSink,
ObservationError, observe, error_event, analyze и read_jsonl; типы наблюдений —
Observer, ObservationSink и ObservationValue. Leader/grants/store,
wire records и алгоритм R1/R2 остаются внутренними деталями пакета.

В control_types определены ControllerError, InvalidArguments,
CommandOutcomeUnknown, DeviceControlBusy, ControlOwnershipRequired, RateLimited
и ControlSession, а также общие helpers валидации и UUIDFactory = Callable[[], UUID]. controller.contracts
реэкспортирует те же объекты, без копирования сессий или новых типов ошибок.
Другие ошибки контроллера наследуются от того же ControllerError.

CoordinationGate сохраняет try_acquire, abandon_request, validate и release.
observation_context остаётся optional hook; close не является обязательным
методом gate. MetadataPort содержит read_metadata, read_metadata_field и
update_metadata с optional per-call timeout_seconds; ZontAdapter реализует его
структурно. CoordinationSettings передаёт delta_seconds, timing: LeaseTiming,
profile/previous_profile и poll/retry/jitter/renewal; Clock — now() и
wait(cancelled, seconds). Runtime получает прежние adapter/config/clock/statistics
и необязательные worker_factory и uuid_factory; пакет не читает конфиги и не открывает транспорт.
Точная assembly seam и порядок закрытия — [архитектура, §1](architecture.md#1-состав-и-границы).
Controller API_VERSION=1 сохранён; внутренний wire заменён несовместимым v2.

create_application(uuid_factory=...) передаёт один источник UUID в runtime
координации, локальный исполнитель, менеджер запросов и runtime алгоритмов.
Тот же аргумент доступен у CoordinationRuntime, GrantCoordinator, ExecutorGate,
CommandExecutor, AlgorithmRegistry и AlgorithmRuntime для изолированных проверок.
ControlRequestManager использует свой существующий uuid_factory.
При отсутствии зависимости остаётся uuid4. Фабрика возвращает UUID, а не строку;
она должна быть потокобезопасной и выдавать новые ненулевые значения, уникальные
для участвующих клиентов. Фиксированные последовательности предназначены для
изолированных офлайн-проверок, а не совместной работы с устройством. DI не меняет
точки генерации ID и не делает порядок планирования потоков детерминированным.
Term_id, clock_id и record_id также получают UUID из того же источника;
отдельной фабрики старого token нет.

R1=name содержит лидера, текущий+один будущий grant, отказ и handled_request;
пишут только лидер/кандидаты. R2=notes содержит один acquire/heartbeat/finish/cancel;
пишут только исполнители, лидер только читает. Даже локальные роли общаются
через регистры. Без лидера выдачи нет. Неизвестные/v1/повреждённые данные и
профили отклоняются без очистки; новый запуск на исходном name/пустых notes
не требует миграции. Точные поля UUID/сроков, A→B и условные гарантии —
[контракт протокола](../reference/leader-election.md).

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
актуальные проверки. acquire_control либо request_control с получением GRANTED
и release_control задают границу монополии. try_acquire_control остаётся
непосредственной попыткой: сессия либо DeviceControlBusy, без ожидания grant.
Без монополии — ошибка без команды ZONT.
Разные приборы допускают параллельное управление, чтения/preview не блокируются.

Принятая команда завершается независимо от последующего stop/timeout once.
Это относится и к finally. Монополия удерживается до завершения finally и
принятых команд. Новые команды при локальном истечении grant отклоняются,
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
Ограниченное stop может закончиться раньше потока; общие ресурсы доступны
до cleanup. Kill/смерть процесса вне гарантии finally.

Первый валидный app.close(wait_timeout=...) закрывает вход общего исполнителя
до отмены зарегистрированных алгоритмов. Новые app.controller() и
try_acquire_control, request_control и acquire_control, в том числе на уже выданном фасаде, получают
ControlOwnershipRequired без gate/HTTP. Существующая действующая сессия,
для которой ещё не запрошен release, сохраняет обычное право execute_command
и finally до истечения grant. Shutdown не продлевает аренду и не отменяет уже
принятые команды. Начавшийся до shutdown acquisition учитывается в ожидании;
сессия, не зафиксированная до закрытия входа, потребителю не выдаётся и проходит
cleanup разрешения внутри исполнителя.

После отмены всех runtimes shutdown ждёт их finally, затем executor.wait_idle:
получение монополии, сессии между командами, validation/send и release.
Только после этого закрываются координация, транспорт и принадлежащий
приложению ресурс статистики. Пустой фасад не задерживает shutdown.
Произвольные внешние чтения без сессии отдельно не учитываются.

Внешний владелец app.controller() сам получает сигнал завершения, заканчивает
свою секцию и вызывает release_control в finally; регистратор не отменяет его
поток. Срок lease_seconds ограничивает также локальное владение:
idle-сессия после expiry удаляется при следующем захвате или shutdown drain
без сетевой записи. Это позволяет завершить close и после потери session
при прерывании acquisition. Незавершённый acquisition (включая получение
контекста статистики), validation/send и запрошенный release ждём до конца;
expiry не отменяет принятую работу. Если release завершился ошибкой,
reservation сохраняется до локального expiry и окончания выполняющейся работы,
без сетевого повтора. Старый session после удаления не получает прав на команды.

close(wait_timeout=None), в том числе выход из with, ждёт без ограничения.
Вызов из потока, который всё ещё удерживает собственную сессию, будет ждать
его же release или expiry: сначала cleanup/release либо ограниченный close, затем cleanup
и полный close. Ограниченный close возвращает False, если работа ещё не
закончена, и сохраняет ресурсы; повторные и конкурентные close ждут один
shutdown. True означает успешное завершение, ошибка не превращается в True.

Ранний DI-hook begin_close_controller выполняется один раз и без I/O.
Его ошибка терминальна: новые старты запрещены, отмена runtimes опубликована,
событие результата установлено, все close получают безопасный
RuntimeError('Shared controller shutdown failed'), включая wait_timeout=0.
Shutdown-поток и завершающий hook не запускаются, ресурсы остаются открытыми
для cleanup действующих владельцев в пределах аренды. Повторный close не
повторяет hook; событие результата с ошибкой не доказывает закрытия ресурсов.

Прямой ЛК остаётся с принятым человеком риском. Занятость видна по grants в name/R1
с исходным префиксом, но это не техническая блокировка ЛК и не доказательство
авторства команды. Ручной on во время импульса не отменяет его finally off.

Уже подтверждённый grant хранится с причинным ClockSample и исходным локальным
интервалом `[usable_from, expires_at)`. Время wire — UNIX epoch по фиксированной
BOOTTIME-шкале лидера, не wall-clock исполнителя. Смена текущего лидера не
отзывает grant и не обновляет его проекцию; после restart права без исходной
привязки не восстанавливаются. Перед каждым grant отдельно подтверждается
renewal лидера и horizon `leader_until >= valid_until+Delta`. Между grants
резерв до `valid_until+K(Delta_cmd)*Delta_cmd`; finish/cancel его не сокращают.
Память nonce всего срока и handled_request предотвращают повторную выдачу
старой попытки после expiry/вытеснения/takeover. Лидер при остановке не пишет
ранний FREE; старые сроки истекают естественно.

## Запрос монополии API v1

Все четыре метода доступны на привязанном Controller:

```python
request_control(device_id: int, *, lease_seconds: float, timeout: float,
                poll_seconds: float = 1, cancelled: Event | None = None) -> ControlRequest
get_control_request(request: ControlRequest) -> ControlRequestResult
cancel_control_request(request: ControlRequest) -> ControlRequestResult
acquire_control(device_id: int, *, lease_seconds: float, timeout: float,
                poll_seconds: float = 1, cancelled: Event | None = None) -> ControlRequestResult
```

Event — threading.Event. device_id — положительный int; длительности — конечные
положительные int/float, bool запрещён; timeout <= lease_seconds. cancelled —
Event либо None. Неверные аргументы и ограничения нижнего gate проверяются
до worker/I/O: InvalidArguments. Закрытый вход — ControlOwnershipRequired
без handle и I/O. У уже выданной сессии expires_at должен помещаться в новую
аренду, иначе создание запроса отклоняется. Если сессия появилась позже из
параллельного immediate, это проверяется повторно перед reuse: несовместимый
срок даёт DENIED/CONTROLLER_ERROR/invalid_arguments, сохраняя прежнюю сессию.

request_control локально регистрирует запрос и запускает worker, не ждёт
gate/HTTP. ControlRequest — неизменяемый identity handle с request_id: UUID,
device_id и initial_state=REQUESTED. initial_state не меняется, даже если
первый get уже видит GRANTED. Для одного binding/device повторный REQUESTED
с теми же lease/timeout/poll и тем же объектом cancelled возвращает тот же
handle; другие параметры дают InvalidArguments. После terminal новый вызов
создаёт новый request_id. UUID или сконструированный DTO не дают полномочий:
get/cancel принимают только точный выданный handle своего DI-binding.
Чужой/поддельный handle — InvalidArguments без I/O.

ControlRequestState: REQUESTED='requested', GRANTED='granted', DENIED='denied'.
Неизменяемый ControlRequestResult содержит request_id, device_id, state,
session, reason, error_code:

| state | session | reason / error_code |
| --- | --- | --- |
| REQUESTED | None | None / None |
| GRANTED | Точный объект ControlSession своего прибора | None / None |
| DENIED | None | Обязательная reason; error_code только при CONTROLLER_ERROR |

Заранее опубликованный будущий grant остаётся REQUESTED с session=None до
usable_from, без нового публичного типа. Если исходный timeout/lease наступил
раньше, handle становится DENIED и поздний grant caller не раскрывается.
Вытеснение R2 или смена срока до получения grant допускает повтор с исходным
request_id; новый срок требует свежего nonce, не новых локальных бюджетов.
Get/cancel локальны; cancel не отзывает уже опубликованную бронь и не меняет
победивший terminal GRANTED. Подтверждённый future grant не переиздаётся
из-за смены текущего лидера.

ControlRequestReason: TIMEOUT='timeout', CANCELLED='cancelled',
SHUTDOWN='shutdown', LEASE_EXPIRED='lease_expired',
CONTROLLER_ERROR='controller_error'. ControlRequestErrorCode задаёт безопасное
соответствие: authentication_error → AuthenticationError, device_unavailable →
DeviceUnavailable, unsupported_action → UnsupportedAction, invalid_arguments →
InvalidArguments, incomplete_history → IncompleteHistory, rate_limited →
RateLimited, command_outcome_unknown → CommandOutcomeUnknown, device_control_busy →
DeviceControlBusy, control_ownership_required → ControlOwnershipRequired,
controller_error → ControllerError. Неизвестный тип ошибки, включая BaseException,
классифицируется как controller_error. Result не хранит exception, traceback,
cause/context, credentials или текст ответа. make_error() возвращает None без
error_code; иначе каждый вызов создаёт новый безопасный ControllerError нужного
типа без аргументов. Его можно поднять, не меняя DTO и weak registry.

get/cancel работают локально, без gate/HTTP, повторной публикации и renew.
get может зафиксировать уже наступивший отказ при зависшем HTTP. Terminal
не меняется: повторные get/cancel возвращают тот же объект результата и session.
GRANTED остаётся фактом выдачи после release/expiry/close; execute_command
проверяет действующее право отдельно. Отмена после GRANTED не отзывает сессию:
владелец сам завершает секцию и release. Гонку grant/cancel решает общий lock.

acquire_control использует тот же запрос и возвращает только GRANTED/DENIED.
При timeout/cancel фиксирует отказ, даже пока worker ждёт отправленный HTTP;
caller не ждёт его завершения. Ошибки admission выбрасываются до создания
запроса, а CONTROLLER_ERROR в результате обрабатывается через make_error().

### Время, worker и поздний cleanup

Локальные BOOTTIME-часы общие для исполнителя и gate, без wall-clock/NTP.
При создании фиксируются created_at, wait_deadline=created_at+timeout,
lease_deadline=created_at+lease_seconds и attempt_deadline=min(wait_deadline,
lease_deadline). Очередь, polling, повторная публикация и ack их не переносят.
poll_seconds — интервал gate-попыток, а не timeout и не новый срок аренды;
timeout короче poll_seconds допустим. Session.expires_at — минимум нижнего
срока подтверждённого grant R1 и исходного lease_deadline; это тот же объект у gate
и исполнителя. Аренду на grant, всю секцию, finally и запас выбирает потребитель.

Busy, DeviceUnavailable и RateLimited оставляют REQUESTED до следующего poll;
CommandOutcomeUnknown при публикации запроса R2 оставляет gate в uncertain и допускает
чтение ack без слепого повтора записи. Иная ошибка даёт DENIED/CONTROLLER_ERROR.
При одновременных причинах приоритет SHUTDOWN, CANCELLED, LEASE_EXPIRED, TIMEOUT.
Равенство deadline уже означает отказ. Просроченная полученная session даёт
LEASE_EXPIRED. После terminal причина не меняется; поздний grant не оживляет DENIED.
Эти повторы относятся к получению разрешения; физические команды не повторяются.

У каждого прибора — ленивый worker и локальная FIFO-очередь на общей Condition
исполнителя; другие приборы продвигаются независимо. Вне блокирующего callback
сроки и Event проверяются с ожиданием не более 0.1 с. Во время HTTP единственный
worker не обслуживает очередь: без get/sync/cancel отказ фиксируется после
возврата попытки. Polling статуса не нужен для продвижения или cleanup.
Пустая очередь и собственная регистрация worker удаляются атомарно под той же
Condition: новая заявка подхватывается прежним worker либо создаёт следующего.
У одного прибора gate-попытки не пересекаются. Завершённые Thread не архивируются
сильно; WeakSet позволяет shutdown дождаться и уходящего потока.

Активная очередь удерживает handle до terminal и окончания cleanup, затем
terminal registry хранит его слабо; значение содержит UUID/result, не handle.
Потребитель удерживает handle для дальнейшего чтения. Сборка handle удаляет
запись без forget/TTL/БД. Terminal handles сами по себе не задерживают close.

Новая session сначала unclaimed: immediate/reuse/execute её не раскрывают,
expiry/drain не удаляют её до решения manager. Под общей Condition она становится
issued вместе с GRANTED либо closing для cleanup отказа. Reuse старой issued
session проверяет точную reservation, binding, identity, срок и отсутствие
release/closing; не сокращает и не продлевает её. Cleanup отказанного запроса
касается только созданной им reservation и не отзывает прежнюю issued-session.

Timeout/cancel/close не прерывают принятый HTTP. Поздний результат и прерывание
после сохранения unclaimed завершаются первой попыткой release и локальным
abandon; неизвестный/ошибочный release не повторяется, reservation остаётся до
исходного expiry. begin_close локально закрывает вход и даёт pending-запросам
DENIED/SHUTDOWN. wait_idle ждёт acquisition, workers, late cleanup, команды и
release до закрытия координации/транспорта; уже выданный GRANTED не отзывается.

Deadline проверяется между стадиями ленивого создания устройства: metadata,
read_r1, read_r2, публикацией пары gate/worker и запуском нового coordinator.
Начатая составная стадия может закончиться поздно, следующая после границы
не начинается. Ранее работающий coordinator независим от timeout заявки.
Эти проверки обеспечивают срок API и не доказывают строгую монополию ZONT.
Принятый A04 допускает потерю idle-session до возврата handle с ожиданием expiry;
C03 запрещает новому запросу наследовать отказанные session/ack или новый lease.
Причины новых нарушений сверяются с [реестром R/L/A/C](../reference/coordination-failure-scenarios.md),
а не объявляются принятыми по одному совпавшему результату.

## Конфиги и секреты

Файлы относительно cwd: config/controller.json, config/registry.json,
config/\<algorithm_id\>.json. Последний читает и проверяет сама фабрика модуля;
регистратор не знает module_configs. Общих --config/ZONT_CONFIG нет.
Примеры и рабочие конфиги не включаются в wheel. [Схемы](../../config/README.md).

Controller требует v2 поля delta_cmd_seconds/profile_id и pacing. Пример
5/5/300 с — unmeasured безопасность; poll 20/retry 60/jitter 0.25/renewal 0.25 —
provisional runtime, отдельно от результатов симуляционной сетки. Профиль
безопасности меняется согласованным перезапуском с полным previous_profile A
и новым UUID B: дождаться A, записать R1 B, затем обычный запрос R2 B.
При горизонте старой аренды 300 с наблюдение ≈331.58 с плюс накладные расходы;
один request 90 с takeover не обеспечивает. Уже запущенный coordinator живёт
независимо от terminal bootstrap; новый запрос имеет новые фиксированные сроки.

ZONT_TOKEN — только окружение; контакт приложения — controller.json.
Интерактивная утилита токена имеет закрытый ввод пароля. Не выводить секреты
или произвольные тела API-ошибок. Облачный Lockbox и деплой остаются планом.
