import pytest

from app.chpl_parser import (NO_SECTION, NO_TEXT, UNKNOWN_SUBSTANCE, parse_section, parse_text,
                             skladniki_dla_mocy)

# Oczekiwany status dla prawdziwych ChPL ze startowym słownikiem
EXPECTED_REVIEW = {"45141", "45157", "45167", "nurofen_forte"}


def names(result, moc=None):
    items = result.substancje if moc is None else skladniki_dla_mocy(result, moc)
    return {e.nazwa for e in items}


def test_all_real_sections_parse(tables, sections):
    for key, text in sections.items():
        r = parse_section(text, tables)
        assert (r.status != "OK") == (key in EXPECTED_REVIEW), (key, r.powody, r.nieznane)
        assert not any(n.endswith(":") for n in names(r)), key
        assert not any("tabletki" in n or "kapsułki" in n for n in names(r)), key


def test_labels_skipped_and_duplicates_merged(tables, sections):
    r = parse_section(sections["11104"], tables)  # Ibuprom Max
    assert names(r) == {"laktoza", "powidon", "skrobia kukurydziana", "talk", "kroskarmeloza sodowa",
                        "magnezu stearynian", "krzemionka koloidalna bezwodna", "sacharoza", "tytanu dwutlenek",
                        "wosk Carnauba", "wosk pszczeli biały"}


def test_comma_inside_name_is_not_split(tables, sections):
    r = parse_section(sections["45106"], tables)
    assert "sorbitol" in names(r)                              # "Sorbitol ciekły, częściowo odwodniony (E 420)"
    assert "częściowo odwodniony" not in names(r)


def test_dose_headers_for_combination_product(tables, sections):
    """Regresja: zakresy mocy nie mogą się przenosić między substancjami."""
    r = parse_section(sections["45144"], tables)  # Ramidilan HCT, 4 moce
    common = {"celuloza mikrokrystaliczna", "wapnia wodorofosforan", "skrobia żelowana",
              "karboksymetyloskrobia sodowa", "sodu stearylofumaran", "tytanu dwutlenek", "żelatyna",
              "żelaza tlenek czerwony"}
    assert names(r, "5 mg + 5 mg + 12,5 mg") == common | {"żelaza tlenek czarny"}
    assert names(r, "5 mg + 5 mg + 25 mg") == common | {"żelaza tlenek żółty"}
    assert names(r, "10 mg + 5 mg + 25 mg") == common | {"żelaza tlenek żółty"}
    assert names(r, "10 mg + 10 mg + 25 mg") == common | {"żelaza tlenek żółty", "żelaza tlenek czarny"}
    assert skladniki_dla_mocy(r, "20 mg + 5 mg + 25 mg") is None   # moc spoza nagłówków -> weryfikacja


def test_inline_dose_notes(tables, sections):
    r = parse_section(sections["45129"], tables)  # Quetiapine: "(dla tabletek 25 mg, 100 mg)" itd.
    assert {"żelaza tlenek żółty", "żelaza tlenek czerwony"} <= names(r, "25 mg")
    assert "szelak" not in names(r, "25 mg")
    assert {"szelak", "żelaza tlenek czarny", "żelaza tlenek żółty"} <= names(r, "100 mg")
    assert "żelaza tlenek czerwony" not in names(r, "100 mg")
    assert not names(r, "50 mg") & {"szelak", "żelaza tlenek żółty", "żelaza tlenek czerwony"}


def test_dose_header_lines(tables, sections):
    r = parse_section(sections["45142"], tables)  # "267 mg tabletka" / "801 mg tabletka"
    assert "żelaza tlenek żółty" in names(r, "267 mg")
    assert "żelaza tlenek czarny" in names(r, "801 mg")
    assert "żelaza tlenek czarny" not in names(r, "267 mg")
    assert skladniki_dla_mocy(r, "400 mg") is None


def test_premix_with_composition_is_expanded(tables, sections):
    r = parse_section(sections["45167"], tables)  # "Aromat pomarańczowy (zawiera olejek pomarańczowy, etanol, wodę)"
    assert "aromat pomarańczowy" not in names(r)
    assert {"olejek pomarańczowy", "etanol"} <= names(r)
    assert UNKNOWN_SUBSTANCE in r.powody


def test_declared_no_excipients(tables, sections):
    r = parse_section(sections["45184"], tables)
    assert r.status == "OK" and r.brak_substancji and not r.substancje


@pytest.mark.parametrize("text, expected", [
    ("Rdzeń tabletki: celuloza mikrokrystaliczna, kroskarmeloza sodowa, laktoza jedno-\nwodna, magnezu stearynian.\n"
     "Otoczka: hypromeloza, makrogol 400, tytanu dwutlenek (E 171), talk, magnezu\nstearynian",
     {"celuloza mikrokrystaliczna", "kroskarmeloza sodowa", "laktoza", "magnezu stearynian", "hypromeloza",
      "makrogol", "tytanu dwutlenek", "talk"}),
    ("Skrobia kukurydziana, powidon K-30, sodu laurylosiarczan oraz kwas stearynowy.\n"
     "Otoczka Opadry II 85F18422 (alkohol poliwinylowy, tytanu dwutlenek (E 171),\nmakrogol 3350, talk), E 172",
     {"skrobia kukurydziana", "powidon", "sodu laurylosiarczan", "kwas stearynowy", "alkohol poliwinylowy",
      "tytanu dwutlenek", "makrogol", "talk", "żelaza tlenki"}),
    ("Magnezu stearynian (E 572)\nStearynian magnezu", {"magnezu stearynian"}),
])
def test_older_comma_lists(tables, text, expected):
    r = parse_section(text, tables)
    assert r.status == "OK", r.powody
    assert names(r) == expected


def test_unknown_label_with_colon_is_skipped_and_flagged(tables):
    r = parse_section("Warstwa wewnętrzna:\nTalk\nSacharoza", tables)
    assert names(r) == {"talk", "sacharoza"}
    assert "warstwa wewnętrzna:" in r.nieznane


def test_premix_from_dictionary(tables):
    from dataclasses import replace
    t = replace(tables, premixes={"tusz opacode s-1-15094": []})
    r = parse_section("Talk\ntusz Opacode S-1-15094\nSzelak", t)
    assert r.status == "OK" and names(r) == {"talk", "szelak"}


def test_whole_text_errors(tables):
    assert NO_TEXT in parse_text("   ", tables).powody
    assert NO_SECTION in parse_text("Tekst bez punktu sześć " * 20, tables).powody


# --- informacja o produkcie EMA: kilka charakterystyk w jednym dokumencie ---------------
from pathlib import Path as _Path

from app.chpl_parser import eu_numbers, parse_text as _parse_text, reparse, split_smpc, wybierz_czesc

EMA_TEXT = (_Path(__file__).parent / "fixtures" / "ema_informacja_o_produkcie_TEST.txt").read_text(encoding="utf-8")


def test_eu_number_ranges():
    assert eu_numbers("EU/1/97/046/001-003, EU/1/97/046/010") == \
        ["EU/1/97/046/001", "EU/1/97/046/002", "EU/1/97/046/003", "EU/1/97/046/010"]
    assert eu_numbers("EU/1/97/046/004 – EU/1/97/046/006") == \
        ["EU/1/97/046/004", "EU/1/97/046/005", "EU/1/97/046/006"]


def test_ema_document_is_split_into_annex_i_parts(tables):
    parts = split_smpc(EMA_TEXT)
    assert len(parts) == 3                                    # etykiety z Aneksu III się nie liczą
    r = _parse_text(EMA_TEXT, tables)
    assert r.status == "OK" and [c["nazwa"] for c in r.czesci] == \
        ["Aprovel 75 mg tabletki", "Aprovel 150 mg tabletki", "Aprovel 300 mg tabletki"]
    assert "EU/1/97/046/005" in r.czesci[1]["eu"] and "EU/1/97/046/005" not in r.czesci[0]["eu"]


def test_part_selected_by_eu_numbers_then_by_power(tables):
    r = _parse_text(EMA_TEXT, tables)
    by_eu = wybierz_czesc(r, ["EU/1/97/046/011"], "150 mg")
    assert "krzemionka koloidalna bezwodna" in names(by_eu)
    by_power = wybierz_czesc(r, [], "300 mg")                 # brak numerów EU: po mocy z punktu 1
    assert "hypromeloza" in names(by_power)
    assert wybierz_czesc(r, [], "30 mg") is None              # "30 mg" nie może trafić w "300 mg"


def test_reparse_keeps_parts(tables):
    r = _parse_text(EMA_TEXT, tables)
    again = reparse(r.to_dict(), None, tables)
    assert [c["nazwa"] for c in again.czesci] == [c["nazwa"] for c in r.czesci]
    assert again.czesci[1]["eu"] == r.czesci[1]["eu"]
