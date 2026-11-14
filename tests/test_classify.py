from dp.catalog.classify import _fill_seasons, _season_by_date, classify, season_of


def test_episode_title() -> None:
    p = classify("DRŽAVNI POSAO [HQ] - Ep.2036: Na potpis (25.09.2023.)", [], 731)
    assert (p.kind, p.number, p.title, p.air_date) == ("episode", 2036, "Na potpis", "2023-09-25")


def test_episode_title_with_colon_and_parens_in_name() -> None:
    p = classify("DRŽAVNI POSAO [HQ] - Ep.1121: Braco 2: Povratak (15.11.2018.)", [], 600)
    assert (p.number, p.title) == (1121, "Braco 2: Povratak")


def test_early_title_variants() -> None:
    p = classify("DRŽAVNI POSAO - Ep.96: Gaće (11.02.2013.)", [], 450)
    assert (p.kind, p.number, p.title) == ("episode", 96, "Gaće")
    p = classify("DRŽAVNI POSAO [HQ] - Ep.63: Rtanj (19.12.2012.) (Rtanj)", [], 450)
    assert (p.number, p.title, p.air_date) == (63, "Rtanj", "2012-12-19")


def test_non_episodes() -> None:
    assert classify("GEGOVI | Koji je datum?", ["GEGOVI / CENZURISANO"], 334).kind == "gag"
    assert classify("PRAVI FUDBAL [HQ] - Ep.25 (27.05.2017.)", [], 2093).kind == "other_show"
    assert classify("DRŽAVNI POSAO [HQ] - Novogodišnji specijal (31.12.2025.)", [], 2505).kind == (
        "special"
    )
    assert classify("DRŽAVNI POSAO [najava] - predstava u Beogradu", [], 57).kind == "trailer"
    assert classify("GATAR #drzavniposao", ["Šortsi"], None).kind == "short"
    assert classify("Nervozni Boškić", [], 74).kind == "trailer"
    assert classify("Žal za Milošem", [], 263).kind == "clip"


def test_season_of() -> None:
    assert season_of(["DRŽAVNI POSAO 7 - part 2"]) == 7
    assert season_of(["DRŽAVNI POSAO 14"]) == 14
    assert season_of(["GEGOVI / CENZURISANO"]) is None


def test_fill_seasons_only_when_neighbours_agree() -> None:
    filled = _fill_seasons({1: 1, 2: None, 3: 1, 4: None, 5: 2})
    assert filled == {1: 1, 2: 1, 3: 1, 4: None, 5: 2}


def test_special_title() -> None:
    p = classify("DRŽAVNI POSAO [HQ] - Novogodišnji specijal, 3. deo (31.12.2012.)", [], 338)
    assert (p.kind, p.number, p.title, p.air_date) == (
        "special",
        None,
        "Novogodišnji specijal, 3. deo",
        "2012-12-31",
    )


def test_season_by_date() -> None:
    dated = [("2017-06-30", 5), ("2017-10-02", 6), ("2018-06-15", 6)]
    assert _season_by_date(dated, "2017-12-31") == 6
    assert _season_by_date(dated, "2017-01-01") is None


def test_special_is_case_insensitive_and_needs_no_duration() -> None:
    p = classify("Državni posao - Specijal (31.12.2020.)", [], None)
    assert (p.kind, p.title, p.air_date) == ("special", "Specijal", "2020-12-31")
    p = classify("DRŽAVNI POSAO [HQ] - Novogodišnji SPECIJAL (31.12.2019.)", [], 900)
    assert (p.kind, p.title, p.air_date) == ("special", "Novogodišnji SPECIJAL", "2019-12-31")


def test_episode_title_is_case_insensitive() -> None:
    p = classify("Državni posao [HQ] - ep.7: Mala (01.10.2012.)", [], 450)
    assert (p.kind, p.number, p.title) == ("episode", 7, "Mala")


def test_episode_number_out_of_range_is_a_clip() -> None:
    for n in (0, 10000, 12345):
        p = classify(f"DRŽAVNI POSAO [HQ] - Ep.{n}: X (01.10.2012.)", [], 450)
        assert (p.kind, p.reason, p.number) == ("clip", "episode number out of range", None)
    assert classify("DRŽAVNI POSAO [HQ] - Ep.9999: X (01.10.2012.)", [], 450).number == 9999


def test_invalid_date_gives_no_air_date() -> None:
    p = classify("DRŽAVNI POSAO [HQ] - Ep.5: X (31.13.2012.)", [], 450)
    assert (p.kind, p.number, p.air_date) == ("episode", 5, None)
    p = classify("DRŽAVNI POSAO [HQ] - Ep.5: X (29.02.2013.)", [], 450)
    assert p.air_date is None
    p = classify("DRŽAVNI POSAO [HQ] - Specijal (31.02.2020.)", [], 900)
    assert (p.kind, p.title, p.air_date) == ("special", "Specijal", None)
