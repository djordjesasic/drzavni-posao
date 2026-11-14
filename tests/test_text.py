from dp.text import fold, to_latin


def test_cyrillic_to_latin() -> None:
    assert to_latin("Ђорђе Чварков") == "Đorđe Čvarkov"
    assert to_latin("Љубомир Њего Џеп") == "Ljubomir Njego Džep"


def test_mixed_script_passthrough() -> None:
    assert to_latin("Torbica и Бошкић") == "Torbica i Boškić"


def test_fold() -> None:
    assert fold("Đorđe  Čvarkov") == "djordje cvarkov"
    assert fold("Ћевара Марјановић") == "cevara marjanovic"
    assert fold("Frau Šilovička") == "frau silovicka"


def test_fold_accented_cyrillic() -> None:
    assert fold("свѐ") == "sve"
    assert fold("Ѝ") == "i"
    assert fold("Бо̀шкић") == "boskic"
    assert to_latin("свѐ") == "svѐ"  # to_latin keeps the original letters
