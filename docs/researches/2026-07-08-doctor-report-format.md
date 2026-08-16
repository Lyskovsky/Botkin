# Формат медицинского отчёта для врача — research (#290)

> Ресёрч перед реализацией экспорта данных пользователя в отчёт для врача.
> Дата: 2026-07-08. Решение зафиксировано в [ADR-0008](../architecture/decisions/0008-doctor-report-ips-pdf.md).

## TL;DR

1. **Единый формат «сводки пациента» существует — International Patient Summary (IPS),
   ISO 27269** + реализации HL7 FHIR/CDA. Это модель *состава* (какие секции и в каком
   порядке), а не макет. Берём её как таксономию содержания отчёта.
2. **Сертифицированный обмен (FHIR/CDA IPS, РФ-СЭМД через ЕГИСЗ) — вне scope wellness-бота**
   (нужны аккредитация МИС, ЭЦП, гос-интеграция). Делаем человекочитаемый PDF со структурой IPS.
3. **Apple Health не даёт консолидированного PDF** (экспорт только XML; PDF — лишь для ЭКГ) →
   курируемая PDF-сводка закрывает реальную дыру.

## 1. IPS — International Patient Summary

Электронный экстракт ключевых сведений о пациенте; стандарт **ISO 27269**, реализации
**HL7 FHIR IPS IG** (на FHIR R4) и **HL7 CDA IPS**. Принцип: «минимальный, clinically relevant,
specialty-agnostic» снимок — подходит как сводка из приложения (не заменяет мед-карту).

### Состав секций по уровню обязательности

- **Обязательные (Required):** Проблемы/диагнозы · Аллергии и непереносимости · Лекарства.
- **Рекомендованные:** Прививки · Результаты исследований · Процедуры · Медицинские устройства.
- **Опциональные:** Витальные показатели · Анамнез · Функциональный статус · План ведения ·
  Соц.анамнез · Беременности · Распоряжения · (2025) Alerts, Patient Story.

## 2. Маппинг на данные Botkin

| Секция IPS | Уровень | Botkin | Источник |
|---|---|---|---|
| Проблемы/диагнозы | Required | 🟡 частично | онбординг-анкета / KB (family) |
| Аллергии | Required | 🟡 частично | онбординг-анкета |
| Лекарства | Required | 🟡 ~ | `supplements_log` (добавки), онбординг |
| Результаты | Recommended | ✅ сильно | `blood_tests`/KB (канон kb_schema) |
| Витальные | Optional | ✅ сильно | `blood_pressure_logs`, `weights`, CGM |
| Соц.история | Optional | ✅ | активность, питание |

Вывод: сильные данные Botkin — Recommended/Optional секции; три Required у нас слабые →
показываем честно с пометкой «со слов пользователя», не выдаём пустоту за норму.

## 3. РФ-контекст — СЭМД / ЕГИСЗ

**СЭМД** (структурированный электронный медицинский документ) — нац. формат обмена в **ЕГИСЗ**;
каркас **HL7 CDA R2**. Требует формирования в аккредитованной МИС + ЭЦП + регистрации в ЕГИСЗ →
для wellness-бота неприменимо. Структурно согласуется с IPS (оба на CDA-каркасе: заголовок с
идентификацией + секции с человекочитаемым текстом) — берём общий принцип, не сертификацию.

## 4. Best practices оформления

- Врачи/пациенты выше всего ценят **результаты тестов** (91% «очень полезно») и инструкции плана (89%).
- Кратко, иерархично, по секциям с заголовками; стандартная терминология; каждая запись — с датой и источником.
- Header: идентификация, дата генерации, период данных, дисклеймер.

## 5. Прецедент потребительских приложений

**Apple Health:** «Export All Health Data» = XML (не PDF); шаринг с провайдером — только в
участвующих клиниках США (FHIR). Отдельный PDF — лишь для ЭКГ. Консолидированного PDF-саммари нет.

## 6. Решение (реализация)

Секции IPS в клиническом порядке → человекочитаемый HTML → PDF (weasyprint) → Telegram-документ.
Детали и trade-offs — [ADR-0008](../architecture/decisions/0008-doctor-report-ips-pdf.md).

## Источники

- HL7 FHIR IPS IG — https://www.hl7.org/fhir/uv/ips/ , https://build.fhir.org/ig/HL7/fhir-ips/en/
- IPS / ISO 27269 — https://international-patient-summary.net/iso-27269/
- IHE Wiki — IPS — https://wiki.ihe.net/index.php/International_Patient_Summary_(IPS)
- СЭМД / ЕГИСЗ (HL7 CDA) — https://binavigator.ru/articles/servisy-1s/strukturirovannyy-elektronnyy-meditsinskiy-dokument-semd/ , https://n3health.ru/strukturirovannyj-ehlektronnyj-medicinskij-dokument
- Apple Health — share/export — https://support.apple.com/guide/iphone/share-your-health-data-iph5ede58c3d/ios
- Clinical summary best practices — https://www.getfreed.ai/resources/clinical-summary-template
- Visit-summary helpfulness study (PMC) — https://pmc.ncbi.nlm.nih.gov/articles/PMC7453444/
