# Холодное ревью плана приведения к архитектуре, v2

> Историческое ревью: исходная запись 15.10.2026, последующие решения указаны ниже.
> Упомянутые требования координации отменены
> [решением от 20.10.2026](../history/2026-10-20-remove-synchronization.md);
> действующая [архитектура](../agents/architecture.md) хранится отдельно.
> Первоначальные вердикты и принятые риски сохранены; это не список нынешних нерешённых проблем.

Дата: 15.10.2026. Проверяется план
`docs/plans/completed/20261015005915-architecture-alignment-implementation-plan.md`.
Проверка независимая: предыдущие ревью, история обсуждений, отчёты и память
сессий не использовались. Ссылки из разрешённых документов на такие материалы
не открывались.

## Вердикт

**Можно приступать к реализации по плану. Подтверждённых существенных дефектов
или невыполнимых зависимостей этапов не найдено.** Новый порядок не требует
возвращать отменённые контракты прототипа или отвергнутые архитектурой механизмы.
Дополнительного решения владельца до начала этапа 1 по проверенным материалам
не требуется.

План не является побайтовой спецификацией всех DTO, JSON и событий: он прямо
поручает закрепить эти детали тестами в соответствующих этапах. Это допустимо
для плана реализации. Важные точки такого закрепления перечислены ниже;
они не являются отдельными блокирующими замечаниями.

Вывод относится к соответствию документов и выполнимости локальной разработки.
Он не подтверждает свойства ZONT, безопасность распределённой схемы, физический
результат сброса, выполненный деплой или достигнутые 99.9%.

## Что проверено

Использованы AGENTS.md, исходники `zontcontrol/`, `tests/`, `scripts/`,
pyproject.toml, Makefile, `.github/workflows/checks.yml`, архитектура и четыре
действующих справочника: leader-election, orchestration-risk, boiler-reset,
anomaly-detection. Другие документы проекта не читались.

### Перенос прототипа и новые границы

- Разделение авторизации, HTTP, адаптера, фасада, подготовки команд и CLI
  соответствует `docs/agents/architecture.md:30` и
  `docs/agents/architecture.md:645`. План не строит новый API поверх старого
  монолита: извлечение предусмотрено на этапе 2, окончательное удаление —
  на этапе 15 (`docs/plans/completed/20261015005915-architecture-alignment-implementation-plan.md:330`,
  `docs/plans/completed/20261015005915-architecture-alignment-implementation-plan.md:518`).
- Проверенное поведение данных и команд сохраняется через новые интерфейсы,
  а старые сигнатуры и CLI не превращаются в обязательство совместимости.
  Это согласуется с `docs/agents/architecture.md:40`; затронуты также утилита
  токена, исследовательские скрипты, тесты и wheel
  (`docs/plans/completed/20261015005915-architecture-alignment-implementation-plan.md:517`,
  `docs/plans/completed/20261015005915-architecture-alignment-implementation-plan.md:519`,
  `docs/plans/completed/20261015005915-architecture-alignment-implementation-plan.md:535`).
- История для детектора не заменяется current, не сглаживается и сохраняет
  реальные события сброса между минутами. Это существенно: текущий
  `zontcontrol/monitor.py:95` проверяет состояние на конец периода,
  `zontcontrol/monitor.py:105` сохраняет unknown для усечённого диапазона,
  `zontcontrol/monitor.py:113` проверяет пригодность последних измерений.
  Перенос этих свойств задан этапами 3 и 12
  (`docs/plans/completed/20261015005915-architecture-alignment-implementation-plan.md:344`,
  `docs/plans/completed/20261015005915-architecture-alignment-implementation-plan.md:468`).

### Монополия, команды и жизненный цикл

- Этапы 4 и 7 различают локальное владение и межклиентский grant, связывают
  потребителя через DI и сохраняют in-flight до освобождения
  (`docs/plans/completed/20261015005915-architecture-alignment-implementation-plan.md:356`,
  `docs/plans/completed/20261015005915-architecture-alignment-implementation-plan.md:357`,
  `docs/plans/completed/20261015005915-architecture-alignment-implementation-plan.md:400`).
  Чтение и предпросмотр остаются доступны при занятом приборе.
- Обычная команда синхронна; известная ошибка, успешный ответ и неизвестный
  исход после отправки различаются. Автоматического повтора управления нет
  (`docs/plans/completed/20261015005915-architecture-alignment-implementation-plan.md:153`,
  `docs/plans/completed/20261015005915-architecture-alignment-implementation-plan.md:333`).
  Это переносит требуемое поведение нынешнего запрета повторов
  из `zontcontrol/client.py:215`, устраняя смешение исходов старым ZontError.
- Once включает bootstrap и возвращает Timeout независимо от зависшего
  HTTP/потока. Stopping сохраняет экземпляр и монополию до завершения;
  поздний результат не переписывает уже возвращённый Timeout
  (`docs/plans/completed/20261015005915-architecture-alignment-implementation-plan.md:438`,
  `docs/plans/completed/20261015005915-architecture-alignment-implementation-plan.md:439`).
- Continuous не получает ни общего, ни внутреннего deadline регистратора.
  Stop кооперативен; закрытие фасада отложено до cleanup. Это прямо проверяется
  этапом 10 и отдельно для boiler_reset
  (`docs/plans/completed/20261015005915-architecture-alignment-implementation-plan.md:440`,
  `docs/plans/completed/20261015005915-architecture-alignment-implementation-plan.md:441`,
  `docs/plans/completed/20261015005915-architecture-alignment-implementation-plan.md:473`).
- Finally использует обычный API и удерживает локальное владение. При истечении
  R2 новый off может быть отклонён; это явно принятое ограничение
  `docs/reference/leader-election.md:226`, а не новый дефект плана. План
  проверяет эту границу и не вводит обход
  (`docs/plans/completed/20261015005915-architecture-alignment-implementation-plan.md:401`,
  `docs/plans/completed/20261015005915-architecture-alignment-implementation-plan.md:471`).

### R1/R2, время и наблюдения

Роли записи, leader token, nonce, revision, сохранение исходного префикса
и различение FREE/повреждённой записи перенесены. Формулы R2 и cap лидера
соответствуют `docs/reference/leader-election.md:95`,
`docs/reference/leader-election.md:110` и
`docs/reference/leader-election.md:198`. План фиксирует rho по исходному
периоду, независимый запас короткого Delta и начало deadline от отправки
подтверждённого nonce
(`docs/plans/completed/20261015005915-architecture-alignment-implementation-plan.md:179`,
`docs/plans/completed/20261015005915-architecture-alignment-implementation-plan.md:196`).

Наблюдение неизменного R2 начинается с первого ответа, а заканчивается
отправкой последнего совпавшего чтения; повторный ack не продлевает право.
Границы, поздний ack и противоположные погрешности предусмотрены тестами
(`docs/plans/completed/20261015005915-architecture-alignment-implementation-plan.md:398`,
`docs/plans/completed/20261015005915-architecture-alignment-implementation-plan.md:401`).

Известное восстановление старого header R1 и возможные межклиентские конфликты
не объявлены устранёнными. Статистика предусматривает полные эпизоды, oracle
виртуального ресурса, неопределённость и покрытие, раздельный анализ версий
и профилей (`docs/plans/completed/20261015005915-architecture-alignment-implementation-plan.md:95`,
`docs/plans/completed/20261015005915-architecture-alignment-implementation-plan.md:412`,
`docs/plans/completed/20261015005915-architecture-alignment-implementation-plan.md:415`).
Это соответствует `docs/reference/orchestration-risk.md:11` и
`docs/reference/orchestration-risk.md:68`; требовать предварительного
доказательства 99.9% или возвращать recovery-журнал оснований нет.

### Зависимости этапов и поставка

Невыполнимого цикла зависимостей не найдено:

- Этап 4 может проверить timeout/stop как подставное изменение контекста,
  не импортируя ещё не написанный регистратор. Временный gate разрешён только
  в тестах; будущие публичные эффекты не обходят R2
  (`docs/plans/completed/20261015005915-architecture-alignment-implementation-plan.md:359`).
- Этапы 9–11 используют подставные specs/factories; отсутствующие builtins
  импортировать не требуется. Реальные модули добавляются по готовности
  в этапах 12 и 14
  (`docs/plans/completed/20261015005915-architecture-alignment-implementation-plan.md:452`,
  `docs/plans/completed/20261015005915-architecture-alignment-implementation-plan.md:474`,
  `docs/plans/completed/20261015005915-architecture-alignment-implementation-plan.md:502`).
- Ещё не перенесённый код прототипа временно допускается; тесты и сигнатуры
  можно менять вместе с переносом. Поэтому извлечение транспорта на этапе 2
  не вынуждает сохранять совместимый второй клиент ради прохождения тестов на промежуточном этапе
  (`docs/plans/completed/20261015005915-architecture-alignment-implementation-plan.md:77`,
  `docs/plans/completed/20261015005915-architecture-alignment-implementation-plan.md:332`).
- Установленный wheel проверяется вне checkout с отдельным корнем config/;
  пользовательские конфиги и reports не включаются в пакет
  (`docs/plans/completed/20261015005915-architecture-alignment-implementation-plan.md:532`,
  `docs/plans/completed/20261015005915-architecture-alignment-implementation-plan.md:534`).
  Runtime-зависимости не добавляются, деплой и реальный сброс не являются
  результатом офлайн-сборки
  (`docs/plans/completed/20261015005915-architecture-alignment-implementation-plan.md:69`,
  `docs/plans/completed/20261015005915-architecture-alignment-implementation-plan.md:300`,
  `docs/plans/completed/20261015005915-architecture-alignment-implementation-plan.md:563`).

## Что конкретизировать внутри предусмотренных этапов

Это контрольные точки реализации, а не дополнительные архитектурные требования.

1. **Этапы 1 и 3: точная политика неполной истории.** Архитектура одновременно
   объявляет возвращаемый HistoryResult с неполнотой и ошибку IncompleteHistory
   (`docs/agents/architecture.md:68`, `docs/agents/architecture.md:92`,
   `docs/agents/architecture.md:100`). Следует тестами различить эти случаи.
   Например, усечённый температурный архив должен доходить до детектора
   с признаком неполноты и давать прежний unknown, а не терять этот исход
   из-за нового адаптера. Минимальное закрепление: описать в contracts,
   какие неполные данные возвращаются и какие невозможные запросы дают ошибку;
   добавить проход HistoryResult → assess_history на усечённом архиве.
   План уже требует это поведение на строках 344 и 468.
2. **Этапы 5–7: законченный wire format и таблица переходов.** Нельзя оставлять
   в коде только перечень полей из строк 173–176 плана. Для сценария позднего
   finished от старого grant нужны точные типы, идентичность полного record,
   сопоставление request/grant/token и правила обновления revision/nonce.
   Минимальное закрепление: кодек с round-trip и отрицательными примерами,
   переходы в tests/test_executor_grants.py, описание фактического формата
   в действующем справочнике. Это уже предусмотрено строками 369, 396 и 402
   плана; произвольная замена протокола здесь не нужна.
3. **Этап 8: единица эпизода и наблюдаемость.** До выдачи численной оценки нужно
   закрепить начало/конец эпизода и отличие «конфликта не увидели» от
   «наблюдали весь эпизод без конфликта». Иначе серия обычных опросов может
   случайно увеличить знаменатель. Минимальное закрепление: схема событий
   и агрегатор с проверкой по oracle и отдельным неполным эпизодом;
   план это уже задаёт на строках 412 и 415, справочник — на
   `docs/reference/orchestration-risk.md:11` и
   `docs/reference/orchestration-risk.md:74`.

Эти решения можно принимать при реализации в установленных границах.
Их не нужно выносить в новый цикл согласования с владельцем, если не меняется
описанное поведение или принятые гарантии.

## Выполненные проверки и ограничения

- Построена карта определений и импортов разрешённых Python-модулей;
  проверены текущие потребители ZontClient, подготовка/валидация команд,
  нормализация истории, assess_history и исследовательский архив.
- Запущен `python3 -m unittest discover -s tests` с удалёнными из дочернего
  окружения переменными ZONT_*: **95 тестов, OK**. HTTP и команды устройству
  не выполнялись; продукт и автономные алгоритмы не запускались.
- Полный `make check` в этом ревью не запускался: его lint вызывает
  `scripts/check_project.py:78`, который через `scripts/check_project.py:27`
  читает все документы, включая исключённые условиями проверки каталоги.
  Это ограничение состава холодного ревью, не обнаруженная поломка make check.
- Новая реализация отсутствует; прохождение тестов прототипа не доказывает
  работоспособность будущей координации, упаковки или нового CLI. Их проверки
  остаются задачами соответствующих этапов плана.

Код, архитектура и план при ревью не менялись. Создан только этот отчёт.
