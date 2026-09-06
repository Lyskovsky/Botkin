from core.food.plan_prefix import strip_plan_prefix


def test_plan_colon():
    assert strip_plan_prefix("План: 3 яйца, творог 200 г") == ("3 яйца, творог 200 г", True)


def test_na_den():
    assert strip_plan_prefix("на день — тарелка с курицей") == ("тарелка с курицей", True)


def test_planiruyu():
    assert strip_plan_prefix("планирую съесть 2 яйца") == ("2 яйца", True)


def test_no_prefix():
    assert strip_plan_prefix("2 яйца и хлеб") == ("2 яйца и хлеб", False)


def test_word_inside_not_prefix():
    assert strip_plan_prefix("суп по плану врача") == ("суп по плану врача", False)


def test_bare_word_is_not_plan():
    assert strip_plan_prefix("план") == ("план", False)


def test_planerka_not_plan():
    assert strip_plan_prefix("Планёрка: кофе") == ("Планёрка: кофе", False)


def test_voice_caption_equal_to_description_not_duplicated():
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "telegram-bot"))
    from handlers.photo import merge_caption_and_description

    assert merge_caption_and_description("план: суп", "план: суп") == "план: суп"
    assert merge_caption_and_description("", "суп") == "суп"
    assert merge_caption_and_description("фото ужина", "с рисом") == "фото ужина\nс рисом"
    text, is_plan = strip_plan_prefix(merge_caption_and_description("план: суп", "план: суп"))
    assert (text, is_plan) == ("суп", True)
