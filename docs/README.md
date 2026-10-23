# Документация zontcontrol

Текущее приложение использует Controller API v2 без синхронизации управления
прибором. Пользовательский вход — [README](../README.md), обязательные
ограничения для изменений — [AGENTS](../AGENTS.md).

## Пользователю

- [Установка и конфиги](../README.md#установка-и-конфиги),
  [авторизация](../README.md#авторизация), [запуск](../README.md#запуск)
  и [CLI](../README.md#cli).
- [Конфигурация](../config/README.md): фиксированные файлы и их параметры.
- [Автоматический сброс](reference/boiler-reset.md): последовательность,
  остановка и интервалы; [детектор](reference/anomaly-detection.md) объясняет
  условия подтверждения аномалии.

## Разработчику

Перед изменением выберите область в [индексе инструкций](agents/README.md).

- API и предметные данные: [контракты](agents/contracts.md),
  [справочник ZONT](reference/zont-api.md).
- Модули, DI и lifecycle: [архитектура](agents/architecture.md).
- Детекция и калибровка: [детектор](reference/anomaly-detection.md),
  [сброс](reference/boiler-reset.md), [результаты калибровки](reports/2026-10-13-anomaly-calibration.md).
- Проверки и выпуск: [разработка](agents/development.md),
  [технические команды](../scripts/README.md),
  [процедура выпуска](../scripts/README.md#версии-и-выпуск).
- Новая документация и её обновление: [правила ведения](agents/documentation.md).

## Оператору облака

Начните с [пошагового how-to](reference/github-yandex-cloud-howto.md).
[Контракт деплоя](agents/deployment.md) задаёт gate, OIDC, полномочия и проверки;
[облачный вход](agents/contracts.md#облачный-вход) — поведение приложения.
Команды вспомогательных скриптов находятся в [scripts/README](../scripts/README.md#облачный-запуск-и-деплой).

ID, имена и общие/отдельные ресурсы собраны в
[реестре](reference/yandex-cloud-resources.md) с датой и источниками.
Этапы настройки и внешние проверки сохранены в датированных отчётах
[о создании ресурсов](reports/2026-10-20-yandex-cloud-provisioning.md) и
[о приёмке](reports/2026-10-21-yandex-cloud-acceptance.md). Отчёты подтверждают состояние
только на дату проверки. Чтение how-to не
разрешает ручные `yc`; каждый такой запрос подтверждается отдельно владельцем.

## История, результаты и планы

[Исторический индекс](history/README.md) ведёт к основаниям решений,
отменённым схемам, отчётам и ревью; [находки H01–H11](agents/history.md)
связывают конкретные контрпримеры с действующими правилами. Решение
[об удалении синхронизации](history/2026-10-20-remove-synchronization.md)
заменяет старые условия монополии. Отчёты в `docs/reports/` и ревью
в `docs/reviews/` — датированные свидетельства; они не переопределяют контракт.
Выпуски перечислены в [CHANGELOG](../CHANGELOG.md), их основания —
в [истории версий](history/versioning.md).

### Завершённые планы

Это свидетельства исполнения, а не текущие инструкции. Архитектурные планы
могут содержать элементы позднее отменённой координации; действует решение
об удалении синхронизации выше.

- [Выравнивание архитектуры](plans/completed/20261015005915-architecture-alignment-implementation-plan.md).
- [Завершение внешних вызовов](plans/completed/20261016152922-external-controller-shutdown-implementation-plan.md).
- [API запроса управления](plans/completed/20261016204209-control-request-api-implementation-plan.md),
  [изоляция координации](plans/completed/20261017220801-coordination-isolation-implementation-plan.md)
  и [протокол монополии v2](plans/completed/20261019002421-monopoly-protocol-v2-implementation-plan.md)
  — исторические этапы отменённой координации.
- [Предметный вход отопления](plans/completed/20261020120717-typed-heating-input-implementation-plan.md).
- [Облачный once](plans/completed/20261020165020-yandex-cloud-boiler-reset-implementation-plan.md).
- [Порядок в документации](plans/completed/20261021174419-documentation-order-implementation-plan.md).
- [История сброса за два часа и приоритет меток worktime](plans/completed/20261022174030-reset-history-worktime-implementation-plan.md)
  — реализация и итоговое ревью завершены 23.10.2026;
  [постановка и уточнения](plans/completed/20261022174030-reset-history-worktime-request.md).

Исходные постановки, заменённый облачный эскиз и команды bootstrap находятся
в [историческом индексе](history/README.md); действующая процедура — облачный how-to выше.
