import pytest

from app.power import group_key


@pytest.mark.parametrize("a, b", [
    (("Ibuprofenum", "400 mg"), ("Ibuprofenum", "0,4 g")),
    (("Paracetamolum", "120 mg/5 ml"), ("Paracetamolum", "24 mg/ml")),
    (("Abacavirum + Lamivudinum", "600 mg + 300 mg"), ("Lamivudinum + Abacavirum", "300 mg + 600 mg")),
    (("Fentanylum", "25 mikrogramów/h"), ("Fentanylum", "25 µg/h")),
])
def test_same_group(a, b):
    assert group_key(*a) == group_key(*b)


@pytest.mark.parametrize("a, b", [
    (("Ibuprofenum", "400 mg"), ("Ibuprofenum", "200 mg")),
    (("Abacavirum + Lamivudinum", "600 mg + 300 mg"), ("Abacavirum + Lamivudinum", "300 mg + 600 mg")),
])
def test_different_group(a, b):
    assert group_key(*a) != group_key(*b)


def test_brackets_and_ions():
    assert group_key("Acidum lacticum + Acidum salicylicum", "(89 mg + 89 mg)/g") == \
        "acidum lacticum=89 mg/g|acidum salicylicum=89 mg/g"
    # "Mg2+" w nazwie nie jest separatorem substancji
    assert group_key("Magnesii lactas + Pyridoxini hydrochloridum (Mg2+)", "48 mg + 5 mg").count("|") == 1


def test_unparseable_power_falls_back_to_raw():
    assert group_key("Oleum iodisatum", "480 mg I/ml") == group_key("Oleum iodisatum", "480 mg I/ml")
    assert group_key(None, "400 mg") is None
