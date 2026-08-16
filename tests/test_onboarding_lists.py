from core.health.onboarding_lists import (
    split_freetext,
    onboarding_list,
    ALLERGY_KEYS,
    CONDITION_KEYS,
)


def test_split_freetext_keeps_icd_dot_and_comma():
    # Комма внутри пункта сохраняется, когда в строке есть сильный разделитель
    # (конец предложения) для другого пункта — иначе (см. ниже) она сама
    # становится границей (comma-fallback для списков без сильных разделителей).
    assert split_freetext("Астма (J45.0), лёгкая персистирующая. Ринит") == [
        "Астма (J45.0), лёгкая персистирующая",
        "Ринит",
    ]


def test_split_freetext_splits_on_sentence_and_semicolon():
    assert split_freetext("Гипертония; Диабет. Астма") == ["Гипертония", "Диабет", "Астма"]


def test_split_freetext_comma_fallback_for_plain_list():
    assert split_freetext("Гипертония, Диабет") == ["Гипертония", "Диабет"]


def test_onboarding_list_reads_first_nonempty_key():
    data = {"allergies": "", "food_allergies": "Пыльца берёзы"}
    assert onboarding_list(data, ALLERGY_KEYS) == ["Пыльца берёзы"]


def test_onboarding_list_accepts_list_value():
    data = {"chronic_conditions": ["Астма", "Гипертония"]}
    assert onboarding_list(data, CONDITION_KEYS) == ["Астма", "Гипертония"]


def test_onboarding_list_empty_when_missing():
    assert onboarding_list({}, ALLERGY_KEYS) == []
    assert onboarding_list(None, CONDITION_KEYS) == []
