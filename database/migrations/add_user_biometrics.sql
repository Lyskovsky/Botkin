-- Migration: add biometric profile fields to users
-- Required for multi-user medical calculations (BMI, PhenoAge, LE8, Framingham)
-- birth_date → age computation (PhenoAge chrono_age, LE8, Framingham)
-- height_cm  → BMI (LE8 component)
-- sex        → reference ranges, Framingham risk model

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS birth_date     DATE,
    ADD COLUMN IF NOT EXISTS height_cm      SMALLINT,
    ADD COLUMN IF NOT EXISTS sex            VARCHAR(10) DEFAULT 'male';

-- Исторически здесь стоял seed биометрии владельца (дата рождения, рост, пол)
-- с захардкоженным telegram_id. Убрано (#303): ПДн не место в публичном репо,
-- а этот файл — архив эволюции схемы (см. README), он не накатывается.
-- Биометрия заполняется пользователем через онбординг/настройки бота.
