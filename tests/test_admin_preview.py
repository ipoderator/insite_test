"""«Как это увидит клиент»: screens of the scenario drawn for the panel."""
from leadbot.admin import preview
from leadbot.scenario import NODES
from leadbot.texts import TEXTS, TextStore
from tests.sim import label


def flat(messages):
    return "\n".join(text for m in messages for text, _ in m.parts) + "\n" + \
        "\n".join(b for m in messages for b in m.buttons + m.reply_buttons)


def test_every_screen_and_every_text_can_be_previewed():
    texts = TextStore()
    for node_id in NODES:
        assert preview.screen(texts, node_id)
    for key in TEXTS:
        assert preview.text_screen(texts, key)


def test_question_is_bold_with_hint_options_and_back():
    [msg] = preview.screen(TextStore(), "pay")
    assert msg.parts[0] == (label("pay.question"), "b")
    assert label("pay.hint") in msg.parts[1][0]
    assert msg.buttons == [label("pay.opt.yes"), label("pay.opt.no"), label("common.back")]


def test_multi_choice_gets_done_and_intro_comes_first():
    intro, question = preview.screen(TextStore(), "pr_what")
    assert intro.parts == [(label("pr_what.intro"), "")] and not intro.buttons
    assert question.buttons[-2:] == [label("common.done"), label("common.back")]


def test_unsaved_change_is_shown_on_its_screen():
    texts = preview.PreviewTexts(None, {"pay.opt.yes": "Конечно"})
    assert "Конечно" in preview.text_screen(texts, "pay.opt.yes")[0].buttons
    assert preview.text_screen(TextStore(), "pay.opt.yes")[0].buttons[0] == label("pay.opt.yes")


def test_placeholders_get_sample_values():
    text = flat(preview.text_screen(TextStore(), "thanks.text"))
    assert "{name}" not in text and preview.SAMPLE["name"] in text
    privacy = preview.PreviewTexts(None, {"privacy.text": "Без ссылки"})
    [msg] = preview.text_screen(privacy, "privacy.text")
    assert msg.parts == [("Без ссылки\n", ""), (label("privacy.link"), "a")]


def test_phone_screen_has_the_share_button_below():
    question, hint = preview.screen(TextStore(), "fin_phone")
    assert question.reply_buttons == [label("fin_phone.opt.share")] and not question.buttons
    assert hint.buttons == [label("common.back")]


def test_texts_outside_a_screen_are_shown_alone():
    [popup] = preview.text_screen(TextStore(), "err.empty_multi")
    assert popup.popup
    [title] = preview.text_screen(TextStore(), "pay.title")
    assert title.buttons == [label("pay.title")] and not title.parts
    [prompt] = preview.text_screen(TextStore(), "features.other.prompt")
    assert prompt.parts == [(label("features.other.prompt"), "")]
